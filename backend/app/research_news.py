from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from .config import NEWS_DIR
from .storage import read_json, write_json

CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_ROOT = "https://static.cninfo.com.cn/"
SHANGHAI = ZoneInfo("Asia/Shanghai")
Progress = Callable[[int, int | None, str], None]
SEARCH_TERMS = (
    "回购", "增持", "中标", "重大合同", "业绩预增", "扭亏为盈",
    "减持", "立案", "行政处罚", "退市风险", "业绩预亏", "股份冻结",
)
CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Referer": "https://www.cninfo.com.cn/new/fulltextSearch",
}
_POSITIVE_RULES = (
    ("业绩预增", "earnings_up", 1.0), ("扭亏为盈", "turnaround", 1.0),
    ("重大合同", "major_contract", 0.9), ("中标", "bid_award", 0.85),
    ("增持计划", "shareholder_increase", 0.8), ("增持公司股份", "shareholder_increase", 0.75),
    ("回购公司股份方案", "share_buyback", 0.8), ("回购股份方案", "share_buyback", 0.8),
    ("首次回购", "share_buyback", 0.7),
)
_NEGATIVE_RULES = (
    ("退市风险", "delisting_risk", -1.0), ("立案", "investigation", -1.0),
    ("行政处罚", "administrative_penalty", -0.95), ("业绩预亏", "earnings_loss", -0.9),
    ("股份冻结", "share_freeze", -0.85), ("减持计划", "shareholder_reduction", -0.75),
)
_ROUTINE_BUYBACK = ("注销", "限制性股票", "出售已回购", "进展", "实施完成")


def _clean_title(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(str(value or "")))).strip()


def classify_announcement(title: str) -> tuple[str, str, float] | None:
    clean = _clean_title(title)
    if not clean or ("回购" in clean and any(term in clean for term in _ROUTINE_BUYBACK)):
        return None
    if "减持" in clean and any(term in clean for term in ("届满", "实施完毕", "实施完成", "实施结果", "终止")):
        return None
    if "撤销退市风险" in clean:
        return None
    for term, event_type, score in _NEGATIVE_RULES:
        if term in clean:
            return "negative", event_type, score
    for term, event_type, score in _POSITIVE_RULES:
        if term in clean:
            return "positive", event_type, score
    return None


def _symbol(sec_code: Any) -> str | None:
    code = str(sec_code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return None


def normalize_cninfo_announcement(
    item: dict[str, Any], *, content_hash: str | None, first_seen_at: datetime,
) -> dict[str, Any] | None:
    classified = classify_announcement(str(item.get("announcementTitle") or ""))
    symbol = _symbol(item.get("secCode"))
    announcement_id = str(item.get("announcementId") or "").strip()
    adjunct_url = str(item.get("adjunctUrl") or "").strip().lstrip("/")
    timestamp = item.get("announcementTime")
    if not classified or not symbol or not announcement_id or not adjunct_url or not isinstance(timestamp, (int, float)):
        return None
    sentiment, event_type, signal_score = classified
    published = datetime.fromtimestamp(timestamp / 1000, UTC).astimezone(SHANGHAI)
    first_seen = first_seen_at.astimezone(SHANGHAI) if first_seen_at.tzinfo else first_seen_at.replace(tzinfo=SHANGHAI)
    digest_ok = bool(content_hash and re.fullmatch(r"[0-9a-f]{64}", content_hash))
    return {
        "event_id": f"cninfo:{announcement_id}",
        "symbol": symbol,
        "name": str(item.get("secName") or "").strip(),
        "title": _clean_title(item.get("announcementTitle")),
        "event_type": event_type,
        "sentiment": sentiment,
        "signal_score": signal_score,
        "source": "cninfo",
        "source_name": "巨潮资讯网",
        "source_url": f"{CNINFO_STATIC_ROOT}{adjunct_url}",
        "published_at": published.isoformat(),
        "published_precision": "datetime",
        "first_seen_at": first_seen.isoformat(),
        "content_hash": content_hash if digest_ok else None,
        "actionable": digest_ok,
    }


def merge_events(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {str(item.get("event_id")): dict(item) for item in existing if item.get("event_id")}
    for item in incoming:
        event_id = str(item.get("event_id") or "")
        if not event_id:
            continue
        old = merged.get(event_id)
        new = dict(item)
        if old and old.get("first_seen_at"):
            new["first_seen_at"] = old["first_seen_at"]
        merged[event_id] = {**(old or {}), **new}
    return sorted(merged.values(), key=lambda item: str(item.get("published_at") or ""), reverse=True)


def _pdf_hash(news_dir: Path, item: dict[str, Any], client: httpx.Client) -> str | None:
    announcement_id = str(item.get("announcementId") or "").strip()
    adjunct_url = str(item.get("adjunctUrl") or "").strip().lstrip("/")
    if not announcement_id or not adjunct_url:
        return None
    path = news_dir / "raw" / "cninfo" / "pdf" / f"{announcement_id}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    response = client.get(f"{CNINFO_STATIC_ROOT}{adjunct_url}")
    response.raise_for_status()
    content = response.content
    if not content.startswith(b"%PDF"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pdf.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    return hashlib.sha256(content).hexdigest()


def sync_cninfo(
    client: httpx.Client,
    *,
    news_dir: Path = NEWS_DIR,
    lookback_days: int = 3,
    now: datetime | None = None,
    progress: Progress = lambda *_: None,
) -> dict[str, Any]:
    now = now or datetime.now(SHANGHAI)
    now = now.astimezone(SHANGHAI) if now.tzinfo else now.replace(tzinfo=SHANGHAI)
    start = now.date() - timedelta(days=max(1, lookback_days) - 1)
    date_range = f"{start.isoformat()}~{now.date().isoformat()}"
    run_dir = news_dir / "raw" / "cninfo" / now.strftime("%Y%m%dT%H%M%S")
    events_path = news_dir / "events.json"
    status_path = news_dir / "status.json"
    seen: set[str] = set()
    incoming: list[dict[str, Any]] = []
    pages = hashed = pdf_failures = 0
    write_json(status_path, {"state": "running", "started_at": now.isoformat(), "date_range": date_range, "pages": 0, "discovered": 0, "hashed": 0, "pdf_failures": 0, "error": None})
    try:
        for term_index, term in enumerate(SEARCH_TERMS, 1):
            page = 1
            while page <= 100:
                payload = {
                    "pageNum": str(page), "pageSize": "30", "column": "szse", "tabName": "fulltext",
                    "plate": "", "stock": "", "searchkey": term, "secid": "", "category": "",
                    "trade": "", "seDate": date_range, "sortName": "", "sortType": "", "isHLtitle": "false",
                }
                response = client.post(CNINFO_QUERY_URL, data=payload)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("巨潮查询返回格式不是JSON对象")
                pages += 1
                write_json(run_dir / f"{term}-{page:03d}.json", body)
                announcements = body.get("announcements") or []
                for item in announcements:
                    announcement_id = str(item.get("announcementId") or "")
                    if not announcement_id or announcement_id in seen or not classify_announcement(item.get("announcementTitle") or ""):
                        continue
                    seen.add(announcement_id)
                    try:
                        digest = _pdf_hash(news_dir, item, client)
                    except (httpx.HTTPError, OSError):
                        digest = None
                    pdf_failures += int(digest is None)
                    event = normalize_cninfo_announcement(item, content_hash=digest, first_seen_at=now)
                    if event:
                        incoming.append(event)
                        hashed += int(event["actionable"])
                current = {
                    "state": "running", "started_at": now.isoformat(), "date_range": date_range,
                    "pages": pages, "discovered": len(incoming), "hashed": hashed,
                    "pdf_failures": pdf_failures, "current_term": term, "error": None,
                }
                write_json(status_path, current)
                progress(term_index - 1, len(SEARCH_TERMS), f"{term}：{pages}页，发现{len(incoming)}条，PDF成功{hashed}条")
                if not body.get("hasMore") or not announcements:
                    break
                page += 1
            progress(term_index, len(SEARCH_TERMS), f"已完成 {term_index}/{len(SEARCH_TERMS)} 类，发现{len(incoming)}条，PDF成功{hashed}条")
        events = merge_events(read_json(events_path, []), incoming)
        write_json(events_path, events)
        result = {
            "state": "complete", "started_at": now.isoformat(),
            "completed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "date_range": date_range, "pages": pages, "discovered": len(incoming),
            "hashed": hashed, "pdf_failures": pdf_failures, "events": len(events), "error": None,
        }
        write_json(status_path, result)
        return result
    except Exception as exc:
        write_json(status_path, {
            "state": "error", "started_at": now.isoformat(),
            "completed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "date_range": date_range, "pages": pages, "discovered": len(incoming),
            "hashed": hashed, "pdf_failures": pdf_failures, "error": str(exc),
        })
        raise

