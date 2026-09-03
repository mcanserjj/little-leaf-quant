from datetime import datetime
from zoneinfo import ZoneInfo

from app.research_news import (
    classify_announcement,
    merge_events,
    normalize_cninfo_announcement,
    sync_cninfo,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _announcement(announcement_id: str = "1225000001") -> dict:
    return {
        "secCode": "600000",
        "secName": "浦发银行",
        "announcementId": announcement_id,
        "announcementTitle": "关于回购公司股份方案的公告",
        "announcementTime": 1772236800000,
        "adjunctUrl": f"finalpage/2026-02-28/{announcement_id}.PDF",
    }


def test_cninfo_announcement_requires_real_pdf_hash():
    event = normalize_cninfo_announcement(
        _announcement(),
        content_hash="a" * 64,
        first_seen_at=datetime(2026, 3, 1, 9, 30, tzinfo=SHANGHAI),
    )

    assert event is not None
    assert event["symbol"] == "600000.SH"
    assert event["source"] == "cninfo"
    assert event["source_url"].endswith("/1225000001.PDF")
    assert event["content_hash"] == "a" * 64
    assert event["actionable"] is True


def test_unhashed_announcement_is_preserved_but_not_actionable():
    event = normalize_cninfo_announcement(
        {**_announcement("1225000002"), "secCode": "000001", "announcementTitle": "关于收到行政处罚决定书的公告"},
        content_hash=None,
        first_seen_at=datetime(2026, 3, 1, 9, 30, tzinfo=SHANGHAI),
    )

    assert event is not None
    assert event["sentiment"] == "negative"
    assert event["actionable"] is False


def test_title_classifier_rejects_routine_progress_and_completed_reduction():
    assert classify_announcement("关于回购公司股份的进展公告") is None
    assert classify_announcement("关于股东减持计划期限届满暨实施结果的公告") is None
    assert classify_announcement("关于撤销退市风险警示的公告") is None
    assert classify_announcement("关于回购公司股份方案的公告")[0] == "positive"


def test_merge_events_preserves_first_seen():
    old = {"event_id": "cninfo:1", "first_seen_at": "2026-03-01T09:30:00+08:00", "actionable": False}
    refreshed = {"event_id": "cninfo:1", "first_seen_at": "2026-03-02T10:00:00+08:00", "content_hash": "b" * 64, "actionable": True}

    result = merge_events([old], [refreshed])

    assert len(result) == 1
    assert result[0]["first_seen_at"] == old["first_seen_at"]
    assert result[0]["actionable"] is True


class _Response:
    def __init__(self, *, body=None, content=b""):
        self._body = body
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Client:
    def __init__(self):
        self.pdf_requests = 0

    def post(self, url, data):
        return _Response(body={"announcements": [_announcement()], "hasMore": False})

    def get(self, url):
        self.pdf_requests += 1
        return _Response(content=b"%PDF-1.4\nverified announcement")


def test_sync_archives_raw_pdf_and_merges_without_clearing_existing(tmp_path):
    existing = [{"event_id": "cninfo:old", "published_at": "2026-02-01T00:00:00+08:00"}]
    (tmp_path / "events.json").write_text(__import__("json").dumps(existing), encoding="utf-8")
    progress = []
    client = _Client()

    result = sync_cninfo(
        client,
        news_dir=tmp_path,
        lookback_days=3,
        now=datetime(2026, 3, 1, 9, 30, tzinfo=SHANGHAI),
        progress=lambda done, total, message: progress.append((done, total, message)),
    )

    events = __import__("json").loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    assert {item["event_id"] for item in events} == {"cninfo:old", "cninfo:1225000001"}
    assert result["hashed"] == 1
    assert result["events"] == 2
    assert client.pdf_requests == 1
    assert list((tmp_path / "raw" / "cninfo").glob("*/回购-001.json"))
    assert (tmp_path / "raw" / "cninfo" / "pdf" / "1225000001.pdf").exists()
    assert progress[-1][0] == progress[-1][1]

