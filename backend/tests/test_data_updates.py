import pytest
import polars as pl
from datetime import datetime
from zoneinfo import ZoneInfo

import app.data_updates as data_updates


def test_update_does_not_queue_without_hithink_key(monkeypatch):
    monkeypatch.setattr(data_updates, "load_hithink_key", lambda: None)

    with pytest.raises(ValueError, match="请先配置HiThink"):
        data_updates.start_update("financials")


def test_public_news_update_does_not_require_hithink_key(monkeypatch, tmp_path):
    monkeypatch.setattr(data_updates, "load_hithink_key", lambda: None)
    monkeypatch.setattr(data_updates, "DATA_UPDATE_STATUS_PATH", tmp_path / "jobs.json")

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            return None

    monkeypatch.setattr(data_updates.threading, "Thread", FakeThread)

    result = data_updates.start_update("user_data_research_news")

    assert result["state"] == "queued"


def test_news_auto_sync_is_persistent_and_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(data_updates, "NEWS_SYNC_SETTINGS_PATH", tmp_path / "news-settings.json")

    assert data_updates.news_auto_sync()["enabled"] is False
    assert data_updates.set_news_auto_sync(True)["intervalMinutes"] == 10
    assert data_updates.news_auto_sync()["enabled"] is True


def test_index_updater_uses_official_single_index_history_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(data_updates, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data_updates, "HITHINK_RAW_DIR", tmp_path / "raw")
    calls = []
    timestamp = int(datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)

    def fake_get(client, path, params=None):
        calls.append((path, params))
        return {"timestamp": timestamp, "item": [{"date_ms": timestamp, "open_price": 10, "high_price": 11, "low_price": 9, "close_price": 10.5, "volume": 100, "turnover": 1000}]}

    monkeypatch.setattr(data_updates, "_api_get", fake_get)
    result = data_updates._update_index_daily(object(), lambda *_: None)

    assert result["indices"] == 5
    assert len(calls) == 5
    assert all(path == "/api/a-share-index/prices/historical" for path, _ in calls)
    assert all(params["interval"] == "1d" and "adjust" not in params for _, params in calls)


def test_ext_catalog_marks_current_snapshot_instead_of_historical_membership(monkeypatch, tmp_path):
    monkeypatch.setattr(data_updates, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data_updates, "HITHINK_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(data_updates, "_api_get", lambda client, path, params=None: {"timestamp": 1, "item": [{"thscode": f"{params['tag']}.TI", "name": params["tag"]}]})

    result = data_updates._update_ext_data(object(), lambda *_: None)

    assert result["categories"] == 4
    assert (tmp_path / "ext_data" / "index_catalog.parquet").exists()


def test_current_valuations_are_batched_and_replace_the_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(data_updates, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data_updates, "HITHINK_RAW_DIR", tmp_path / "raw")
    symbols = [f"{index:06d}.SZ" for index in range(205)]
    monkeypatch.setattr(data_updates, "_research_symbols", lambda: symbols)
    calls = []

    def fake_get(client, path, params=None):
        batch = params["thscodes"].split(",")
        calls.append(batch)
        return {"timestamp": 1, "item": [{"thscode": symbol, "pe_ttm": 10} for symbol in batch]}

    monkeypatch.setattr(data_updates, "_api_get", fake_get)
    result = data_updates._update_current_valuations(object(), lambda *_: None)

    assert [len(batch) for batch in calls] == [100, 100, 5]
    assert result["validPeTtm"] == 205
    assert pl.read_parquet(tmp_path / "valuations" / "current" / "part.parquet").height == 205


def test_historical_valuations_resume_after_covered_symbol(monkeypatch, tmp_path):
    for day in ("2026-01-02", "2026-01-05"):
        root = tmp_path / "kline_daily_enriched" / f"date={day}"
        root.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600000.SH"]}).write_parquet(root / "part.parquet")
    history = tmp_path / "valuations" / "history"
    history.mkdir(parents=True)
    data_updates.write_json(history / "coverage.json", {"600000.SH": {"coveredTo": "2026-01-05"}})
    monkeypatch.setattr(data_updates, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data_updates, "_research_symbols", lambda: ["600000.SH", "000001.SZ"])
    calls = []

    class Reply:
        error_code = "0"
        error_msg = ""
        def __init__(self, rows=None): self.rows, self.index = rows or [], 0
        def next(self):
            if self.index >= len(self.rows): return False
            self.index += 1
            return True
        def get_row_data(self): return self.rows[self.index - 1]

    class FakeBaoStock:
        def login(self): return Reply()
        def logout(self): return Reply()
        def query_history_k_data_plus(self, code, fields, start, end, **kwargs):
            calls.append((code, start, end))
            return Reply([["2026-01-05", code, "7.5", "1.0", "2.0", "3.0", "1", "0"]])

    monkeypatch.setattr(data_updates.importlib, "import_module", lambda _: FakeBaoStock())
    result = data_updates._update_historical_valuations(object(), lambda *_: None)

    assert calls == [("sz.000001", "2026-01-02", "2026-01-05")]
    assert result["coveredSymbols"] == 2
    assert pl.concat([pl.read_parquet(path) for path in history.glob("batch-*.parquet")])["pe_ttm"].to_list() == [7.5]
