from __future__ import annotations

import importlib
import json
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from .config import DATA_DIR, DATA_UPDATE_STATUS_PATH, HITHINK_BASE_URL, HITHINK_RAW_DIR, NEWS_DIR, NEWS_SYNC_SETTINGS_PATH
from .research_news import CNINFO_HEADERS, sync_cninfo
from .secrets import load_hithink_key
from .storage import invalidate_coverage_cache, now_iso, read_json, write_json

SHANGHAI = ZoneInfo("Asia/Shanghai")
Progress = Callable[[int, int | None, str], None]

DATA_SOURCES: dict[str, dict[str, Any]] = {
    "instruments": {"label": "股票列表", "provider": "HiThink Financial API", "format": "JSON → Parquet", "method": "分页获取沪深 A 股代码表，校验后合并到本地标的表", "updateSupported": True},
    "kline_daily": {"label": "日K", "provider": "HiThink Market Dump", "format": "Parquet", "method": "下载近10个交易日全市场未复权日K，按(symbol,date)去重合并", "updateSupported": True},
    "kline_daily_enriched": {"label": "复权日K", "provider": "本地确定性计算", "format": "Parquet", "method": "由未复权日K与已验证复权因子重算；当前尚未迁入独立计算器", "updateSupported": False},
    "adj_factor": {"label": "除权因子", "provider": "HiThink Market Dump", "format": "Parquet 原始公司行动", "method": "下载现金分红、送股、配股事件；不把事件直接冒充累计复权因子", "updateSupported": True},
    "financials": {"label": "财务数据", "provider": "HiThink Financial API", "format": "JSON → Parquet", "method": "只补齐研究池内少于两期ROE或缺少经营现金流的公司；排除ST、退市、科创、创业板和北交所，保留报告期与披露日", "updateSupported": True},
    "valuations_current": {"label": "当前估值", "provider": "HiThink Financial API", "format": "JSON → Parquet", "method": "每100只批量刷新最新PE TTM等估值；仅用于刷新当日之后生成的选股决策，不倒填历史", "updateSupported": True, "credential": "hithink"},
    "valuations_history": {"label": "历史估值", "provider": "BaoStock", "format": "逐日接口 → Parquet", "method": "免费按股票增量补齐本地回测区间的逐日PE TTM；记录已成功查询区间，重启后只续传未覆盖日期", "updateSupported": True, "credential": None},
    "ext_data": {"label": "行业/概念目录", "provider": "HiThink Financial API", "format": "JSON → Parquet", "method": "按官方 cn_concept/region/tszs/industry 四类目录增量更新；成分为当前快照，不冒充历史成分", "updateSupported": True},
    "kline_index_daily": {"label": "指数日K", "provider": "HiThink Financial API", "format": "JSON → Parquet", "method": "逐只更新上证、深证、创业板、科创50与沪深300日K；指数端点不支持批量历史请求", "updateSupported": True},
    "user_data_research_news": {"label": "场外资讯", "provider": "巨潮资讯网", "format": "原始 JSON + PDF → 事件 JSON", "method": "增量查询近3日公告并缓存PDF；保留来源URL、发布时间与SHA-256，网站查询接口无正式公开REST稳定性承诺", "updateSupported": True, "credential": None},
    "market_intelligence": {"label": "市场情绪/主力代理", "provider": "HiThink Financial API", "format": "JSON", "method": "获取龙虎榜、热股榜与当日异动；仅作公开资金动向代理，不代表完整主力持仓", "updateSupported": True},
}

_lock = threading.Lock()


class HiThinkError(RuntimeError):
    pass


def _status() -> dict[str, Any]:
    return read_json(DATA_UPDATE_STATUS_PATH, {})


def update_status() -> dict[str, Any]:
    return {"sources": DATA_SOURCES, "jobs": _status(), "newsAutoSync": news_auto_sync()}


def news_auto_sync() -> dict[str, Any]:
    saved = read_json(NEWS_SYNC_SETTINGS_PATH, {})
    return {"enabled": bool(saved.get("enabled", False)), "intervalMinutes": 10}


def set_news_auto_sync(enabled: bool) -> dict[str, Any]:
    value = {"enabled": bool(enabled), "intervalMinutes": 10, "updatedAt": now_iso()}
    write_json(NEWS_SYNC_SETTINGS_PATH, value)
    return value


def run_scheduled_news_update() -> None:
    if not news_auto_sync()["enabled"]:
        return
    try:
        start_update("user_data_research_news")
    except ValueError:
        return


def _set_job(key: str, **values: Any) -> None:
    with _lock:
        state = _status()
        state[key] = {**state.get(key, {}), **values}
        write_json(DATA_UPDATE_STATUS_PATH, state)


def _api_get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.get(f"{HITHINK_BASE_URL}{path}", params=params or {})
    response.raise_for_status()
    envelope = response.json()
    if envelope.get("code") != 0:
        raise HiThinkError(f"HiThink code={envelope.get('code')}: {envelope.get('message', '未知错误')}")
    return envelope.get("data") or {}


def test_hithink_connection(key: str | None = None) -> dict[str, Any]:
    token = (key or load_hithink_key() or "").strip()
    if not token:
        raise ValueError("请先填写HiThink Financial API Key")
    with httpx.Client(headers={"X-api-key": token}, timeout=30) as client:
        data = _api_get(client, "/api/a-share/prices/snapshot", {"thscodes": "600519.SH"})
    return {"ok": True, "sample": "600519.SH", "records": len(data.get("item") or []), "timestamp": data.get("timestamp")}


def _raw_json(kind: str, value: Any) -> Path:
    path = HITHINK_RAW_DIR / kind / f"{datetime.now(SHANGHAI):%Y%m%d-%H%M%S}.json"
    write_json(path, value)
    return path


def _write_parquet(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.write_parquet(temporary)
    temporary.replace(path)


def _merge_frame(path: Path, new: pl.DataFrame, keys: list[str]) -> int:
    if path.exists():
        old = pl.read_parquet(path)
        for name, dtype in old.schema.items():
            if name not in new.columns:
                new = new.with_columns(pl.lit(None, dtype=dtype).alias(name))
        new = new.select(old.columns)
        merged = pl.concat([old, new], how="vertical_relaxed").unique(keys, keep="last")
    else:
        merged = new.unique(keys, keep="last")
    _write_parquet(path, merged)
    return merged.height


def _update_instruments(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    offset, limit = 0, 1000
    while True:
        data = _api_get(client, "/api/meta/tickers/list", {"exchange": "SH,SZ", "asset_type": "a-share", "limit": limit, "offset": offset})
        page = data.get("item") or []
        items.extend(page)
        progress(len(items), data.get("total"), f"已获取 {len(items)} 只")
        if len(page) < limit:
            break
        offset += limit
    if not items:
        raise HiThinkError("股票列表返回空集，未覆盖本地数据")
    _raw_json("instruments", {"source": "hithink", "retrievedAt": now_iso(), "items": items})
    rows = [{"symbol": row.get("thscode"), "name": row.get("name"), "code": row.get("ticker"), "exchange": row.get("exchange"), "region": "CN", "type": "stock", "as_of": datetime.now(SHANGHAI).date()} for row in items if row.get("thscode")]
    current_path = DATA_DIR / "instruments" / "instruments.parquet"
    current = pl.read_parquet(current_path)
    incoming = pl.DataFrame(rows)
    merged = current.join(incoming.select("symbol", pl.col("name").alias("new_name")), on="symbol", how="full", coalesce=True).with_columns(pl.coalesce("new_name", "name").alias("name")).drop("new_name")
    _write_parquet(current_path, merged)
    return {"records": len(items), "message": "股票列表已更新并保留原有扩展字段"}


def _download_dump(client: httpx.Client, kind: str, progress: Progress) -> Path:
    data = _api_get(client, f"/api/dump/market-dumps/{kind}/download-url")
    url = data.get("presigned_url")
    if not url:
        raise HiThinkError("下载接口未返回预签名URL")
    target = HITHINK_RAW_DIR / "market-dumps" / f"{kind}-{datetime.now(SHANGHAI):%Y%m%d-%H%M%S}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".parquet.tmp")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0) or None
        done = 0
        with temporary.open("wb") as stream:
            for chunk in response.iter_bytes():
                stream.write(chunk)
                done += len(chunk)
                progress(done, total, f"已下载 {done / 1024 / 1024:.1f} MB")
    temporary.replace(target)
    return target


def _update_daily(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    source = _download_dump(client, "daily-k-10d", progress)
    frame = pl.read_parquet(source)
    required = {"thscode", "date_ms", "open_price", "high_price", "low_price", "close_price", "volume", "turnover"}
    if not required.issubset(frame.columns):
        raise HiThinkError(f"日K Parquet字段不完整：缺少 {sorted(required - set(frame.columns))}")
    normalized = frame.select(
        pl.col("thscode").alias("symbol"), pl.col("close_price").cast(pl.Float64).alias("close"),
        pl.col("open_price").cast(pl.Float64).alias("open"), pl.col("high_price").cast(pl.Float64).alias("high"),
        pl.col("low_price").cast(pl.Float64).alias("low"), pl.col("volume").cast(pl.Float64),
        pl.col("turnover").cast(pl.Float64).alias("amount"), pl.col("date_ms").cast(pl.Int64).alias("quote_ts"),
        pl.from_epoch("date_ms", time_unit="ms").dt.convert_time_zone("Asia/Shanghai").dt.date().alias("date"),
    )
    dates = normalized["date"].unique().sort().to_list()
    for index, day in enumerate(dates, 1):
        _merge_frame(DATA_DIR / "kline_daily" / f"date={day.isoformat()}" / "part.parquet", normalized.filter(pl.col("date") == day), ["symbol", "date"])
        progress(index, len(dates), f"已合并 {index}/{len(dates)} 个交易日")
    return {"records": normalized.height, "dates": len(dates), "rawPath": str(source.relative_to(DATA_DIR)), "message": "近10个交易日日K已去重合并"}


def _update_adjustment_events(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    source = _download_dump(client, "adjustment-factors", progress)
    frame = pl.read_parquet(source)
    required = {"thscode", "ex_date_ms", "dividend_per_share", "per_share_bonus", "allotment_ratio", "allotment_price"}
    if not required.issubset(frame.columns):
        raise HiThinkError(f"公司行动Parquet字段不完整：缺少 {sorted(required - set(frame.columns))}")
    normalized = frame.select(
        pl.col("thscode").alias("symbol"),
        pl.from_epoch("ex_date_ms", time_unit="ms").dt.convert_time_zone("Asia/Shanghai").dt.date().alias("ex_date"),
        pl.col("dividend_per_share").cast(pl.Float64), pl.col("per_share_bonus").cast(pl.Float64),
        pl.col("allotment_ratio").cast(pl.Float64), pl.col("allotment_price").cast(pl.Float64),
        pl.col("ex_date_ms").cast(pl.Int64),
    )
    records = _merge_frame(DATA_DIR / "adj_factor" / "events.parquet", normalized, ["symbol", "ex_date"])
    return {"records": records, "rawPath": str(source.relative_to(DATA_DIR)), "message": "公司行动原始事件已规范化保存；未把事件冒充日频累计复权因子"}


def _update_ext_data(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    rows = []
    raw = {"source": "hithink", "retrievedAt": now_iso(), "catalogs": {}}
    tags = ("cn_concept", "region", "tszs", "industry")
    for index, tag in enumerate(tags, 1):
        data = _api_get(client, "/api/a-share-index/catalog/ths-index-list", {"tag": tag})
        items = data.get("item") or []
        raw["catalogs"][tag] = data
        rows.extend({"symbol": item.get("thscode"), "name": item.get("name"), "category": tag, "snapshot_at": now_iso()} for item in items if item.get("thscode"))
        progress(index, len(tags), f"已获取 {index}/{len(tags)} 类目录")
    if not rows:
        raise HiThinkError("指数与板块目录返回空集")
    _raw_json("index-catalog", raw)
    frame = pl.DataFrame(rows).unique(["symbol", "category"], keep="last")
    _write_parquet(DATA_DIR / "ext_data" / "index_catalog.parquet", frame)
    return {"records": frame.height, "categories": len(tags), "message": "行业与概念目录已更新；数据标记为当前快照"}


def _milliseconds(day: datetime) -> int:
    return int(day.timestamp() * 1000)


def _update_index_daily(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    codes = ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH")
    end = datetime.now(SHANGHAI)
    start = end.replace(year=end.year - 2)
    rows = []
    raw = {"source": "hithink", "retrievedAt": now_iso(), "indices": {}}
    for index, symbol in enumerate(codes, 1):
        data = _api_get(client, "/api/a-share-index/prices/historical", {"thscode": symbol, "interval": "1d", "start": _milliseconds(start), "end": _milliseconds(end)})
        items = data.get("item") or []
        raw["indices"][symbol] = {"timestamp": data.get("timestamp"), "items": items}
        rows.extend({
            "symbol": symbol, "date": datetime.fromtimestamp(int(item["date_ms"]) / 1000, SHANGHAI).date(),
            "open": _number(item.get("open_price")), "high": _number(item.get("high_price")),
            "low": _number(item.get("low_price")), "close": _number(item.get("close_price")),
            "volume": _number(item.get("volume")), "amount": _number(item.get("turnover")),
        } for item in items if item.get("date_ms") is not None)
        progress(index, len(codes), f"已获取 {index}/{len(codes)} 只指数")
    if not rows:
        raise HiThinkError("指数历史日K返回空集")
    _raw_json("index-daily", raw)
    frame = pl.DataFrame(rows)
    for index, day in enumerate(frame["date"].unique().sort().to_list(), 1):
        _merge_frame(DATA_DIR / "kline_index_daily" / f"date={day.isoformat()}" / "part.parquet", frame.filter(pl.col("date") == day), ["symbol", "date"])
    return {"records": frame.height, "indices": len(codes), "message": "五只基准指数近两年日K已去重更新"}


def _date_of_ms(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, SHANGHAI).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _research_symbols() -> list[str]:
    instruments = pl.read_parquet(DATA_DIR / "instruments" / "instruments.parquet", columns=["symbol", "name"])
    return instruments.filter(
        ~pl.col("name").str.to_uppercase().str.contains(r"ST|退")
        & ~pl.col("symbol").str.starts_with("688")
        & ~pl.col("symbol").str.starts_with("689")
        & ~pl.col("symbol").str.starts_with("300")
        & ~pl.col("symbol").str.starts_with("301")
        & ~pl.col("symbol").str.ends_with(".BJ")
    )["symbol"].sort().to_list()


def _update_current_valuations(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    symbols = _research_symbols()
    rows: list[dict[str, Any]] = []
    raw_batches: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    retrieved_at = now_iso()
    for offset in range(0, len(symbols), 100):
        batch = symbols[offset:offset + 100]
        try:
            data = _api_get(client, "/api/a-share/valuations/snapshot", {"thscodes": ",".join(batch)})
            items = data.get("item") or []
            raw_batches.append({"symbols": batch, "timestamp": data.get("timestamp"), "items": items})
            rows.extend({
                "symbol": item.get("thscode"), "valuation_date": date.fromisoformat(retrieved_at[:10]),
                "retrieved_at": retrieved_at, "provider_timestamp": data.get("timestamp"), "source": "hithink",
                "pe_ttm": _number(item.get("pe_ttm")), "pe_mrq": _number(item.get("pe_mrq")),
                "pb_mrq": _number(item.get("pb_mrq")), "ps_ttm": _number(item.get("ps_ttm")),
                "pcf_ttm": _number(item.get("pcf_ttm")),
            } for item in items if item.get("thscode"))
        except Exception as exc:
            failures.append({"symbols": f"{batch[0]}..{batch[-1]}", "reason": str(exc)})
        done = min(offset + len(batch), len(symbols))
        progress(done, len(symbols), f"已处理 {done}/{len(symbols)}，失败批次 {len(failures)}")
    if not rows:
        raise HiThinkError("当前估值接口未返回任何记录，未覆盖旧快照")
    _raw_json("valuations-current", {"source": "hithink", "retrievedAt": retrieved_at, "batches": raw_batches, "failures": failures})
    frame = pl.DataFrame(rows).unique("symbol", keep="last")
    _write_parquet(DATA_DIR / "valuations" / "current" / "part.parquet", frame)
    valid = frame.filter(pl.col("pe_ttm").is_not_null() & (pl.col("pe_ttm") > 0)).height
    return {"records": frame.height, "validPeTtm": valid, "failedBatches": len(failures), "message": f"当前估值已刷新：{valid}/{len(symbols)} 只具有正PE TTM"}


BAOSTOCK_FIELDS = "date,code,peTTM,pbMRQ,psTTM,pcfNcfTTM,tradestatus,isST"


def _baostock_code(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    return f"{exchange.lower()}.{code}"


def _flush_historical_valuation_batch(rows: list[dict[str, Any]], raw: list[dict[str, Any]], sequence: int) -> None:
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-%f")
    if rows:
        frame = pl.DataFrame(rows).unique(["symbol", "date"], keep="last")
        _write_parquet(DATA_DIR / "valuations" / "history" / f"batch-{stamp}-{sequence:04d}.parquet", frame)
    if raw:
        write_json(DATA_DIR / "raw" / "baostock" / "valuations-history" / f"{stamp}-{sequence:04d}.json", {"source": "baostock.query_history_k_data_plus", "retrievedAt": now_iso(), "records": raw})


def _update_historical_valuations(_client: httpx.Client, progress: Progress) -> dict[str, Any]:
    try:
        bs = importlib.import_module("baostock")
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少baostock依赖，请重新执行web-service.bat让环境自动同步") from exc
    kline_paths = sorted((DATA_DIR / "kline_daily_enriched").glob("date=*/part.parquet"))
    if not kline_paths:
        raise ValueError("没有复权日K，无法确定历史估值补齐区间")
    first_day = kline_paths[0].parent.name.removeprefix("date=")
    last_day = kline_paths[-1].parent.name.removeprefix("date=")
    symbols = _research_symbols()
    state_path = DATA_DIR / "valuations" / "history" / "coverage.json"
    coverage = read_json(state_path, {})
    pending = [symbol for symbol in symbols if str((coverage.get(symbol) or {}).get("coveredTo") or "") < last_day]
    login = bs.login()
    if login is None or str(login.error_code) != "0":
        raise RuntimeError(f"BaoStock登录失败：{getattr(login, 'error_msg', '无响应')}")
    total_rows = 0
    failures: list[dict[str, str]] = []
    batch_rows: list[dict[str, Any]] = []
    batch_raw: list[dict[str, Any]] = []
    try:
        for index, symbol in enumerate(pending, 1):
            previous = (coverage.get(symbol) or {}).get("coveredTo")
            start = max(first_day, (date.fromisoformat(previous) + timedelta(days=1)).isoformat()) if previous else first_day
            try:
                query = bs.query_history_k_data_plus(_baostock_code(symbol), BAOSTOCK_FIELDS, start, last_day, frequency="d", adjustflag="3")
                if query is None or str(query.error_code) != "0":
                    raise RuntimeError(getattr(query, "error_msg", "无响应"))
                source_rows: list[list[str]] = []
                while query.next():
                    source_rows.append(query.get_row_data())
                normalized = []
                fields = BAOSTOCK_FIELDS.split(",")
                for source_row in source_rows:
                    item = dict(zip(fields, source_row, strict=True))
                    normalized.append({
                        "symbol": symbol, "date": date.fromisoformat(item["date"]), "source": "baostock",
                        "retrieved_at": now_iso(), "pe_ttm": _number(item.get("peTTM")),
                        "pb_mrq": _number(item.get("pbMRQ")), "ps_ttm": _number(item.get("psTTM")),
                        "pcf_ttm": _number(item.get("pcfNcfTTM")), "trade_status": item.get("tradestatus"),
                        "is_st": item.get("isST"),
                    })
                batch_rows.extend(normalized)
                batch_raw.append({"symbol": symbol, "code": _baostock_code(symbol), "start": start, "end": last_day, "fields": fields, "rows": source_rows})
                total_rows += len(normalized)
                coverage[symbol] = {"coveredFrom": (coverage.get(symbol) or {}).get("coveredFrom") or start, "coveredTo": last_day, "updatedAt": now_iso()}
            except Exception as exc:
                failures.append({"symbol": symbol, "reason": str(exc)})
            if index % 50 == 0 or index == len(pending):
                _flush_historical_valuation_batch(batch_rows, batch_raw, index // 50 + 1)
                batch_rows.clear()
                batch_raw.clear()
                write_json(state_path, coverage)
            progress(index, len(pending), f"已处理 {index}/{len(pending)}，新增 {total_rows} 行，失败 {len(failures)}")
    finally:
        bs.logout()
    write_json(DATA_DIR / "raw" / "baostock" / "valuations-history" / "last-failures.json", failures)
    return {"requested": len(pending), "rowsAdded": total_rows, "coveredSymbols": len(coverage), "failed": len(failures), "range": [first_day, last_day], "message": f"历史PE TTM增量补齐完成：{len(coverage)}/{len(symbols)}只已查询到{last_day}"}


METRIC_MAP = {"index_weighted_avg_roe": "roe", "total_assets_net_ratio": "roa", "sale_gross_margin": "gross_margin", "sale_net_interest_ratio": "net_margin", "assets_debt_ratio": "debt_to_asset_ratio", "calculate_operating_income_yoy_growth_ratio": "revenue_yoy", "calculate_parent_holder_net_profit_yoy_growth_ratio": "net_income_yoy", "operating_cash_flow_net_divide_income": "operating_cash_to_revenue", "inventory_turnover_ratio": "inventory_turnover"}
CASH_MAP = {"act_cash_flow_net": "net_operating_cash_flow", "invest_cash_flow_net": "net_investing_cash_flow", "financing_cash_flow_net": "net_financing_cash_flow", "pay_fixed_assets_etc_cash": "capex", "cash_equivalents_net_addition": "net_cash_change", "pay_dividends_profits_interest_cash": "pay_dividends_profits_interest_cash"}


def _financial_missing_symbols() -> list[str]:
    instruments = pl.read_parquet(DATA_DIR / "instruments" / "instruments.parquet", columns=["symbol", "name"])
    eligible = instruments.filter(
        ~pl.col("name").str.to_uppercase().str.contains(r"ST|退")
        & ~pl.col("symbol").str.starts_with("688")
        & ~pl.col("symbol").str.starts_with("689")
        & ~pl.col("symbol").str.starts_with("300")
        & ~pl.col("symbol").str.starts_with("301")
        & ~pl.col("symbol").str.ends_with(".BJ")
    )["symbol"].to_list()
    metric_counts = pl.read_parquet(DATA_DIR / "financials" / "metrics" / "part.parquet", columns=["symbol", "period_end", "roe"]).filter(pl.col("roe").is_not_null()).group_by("symbol").agg(pl.col("period_end").n_unique().alias("periods"))
    metrics = metric_counts.filter(pl.col("periods") >= 2)["symbol"].to_list()
    cash = pl.read_parquet(DATA_DIR / "financials" / "cash_flow" / "part.parquet", columns=["symbol", "net_operating_cash_flow"]).filter(pl.col("net_operating_cash_flow").is_not_null())["symbol"].unique().to_list()
    return sorted(set(eligible) - (set(metrics) & set(cash)))


def _update_financials(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    symbols = _financial_missing_symbols()
    metric_rows: list[dict[str, Any]] = []
    cash_rows: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, symbol in enumerate(symbols, 1):
        try:
            income = (_api_get(client, "/api/a-share/financials/income-statements", {"thscode": symbol, "period": "quarterly", "limit": 8}).get("item") or [])
            if not income:
                raise HiThinkError("无最近八期季度利润表")
            raw_indicators = []
            for statement in income:
                period_end = _date_of_ms(statement.get("period_end_ms"))
                if not period_end:
                    continue
                month = int(period_end[5:7])
                quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
                if not quarter:
                    continue
                report = f"{statement.get('fiscal_year')}-{quarter}"
                indicators = _api_get(client, "/api/a-share/financials/indicators", {"thscode": symbol, "report": report})
                values: dict[str, float | None] = {}
                for ability in indicators.get("abilities") or []:
                    for item in ability.get("indicators") or []:
                        field = METRIC_MAP.get(str(item.get("index_id")), str(item.get("index_id")))
                        values[field] = _number(item.get("value"))
                metric_rows.append({"symbol": symbol, "period_end": period_end, "announce_date": _date_of_ms(statement.get("report_date_ms")), "eps_basic": _number(statement.get("basic_eps")), **values})
                raw_indicators.append({"report": report, "data": indicators})
            cash_items = (_api_get(client, "/api/a-share/financials/cash-flow-statements", {"thscode": symbol, "period": "quarterly", "limit": 8}).get("item") or [])
            for item in cash_items:
                cash_rows.append({"symbol": symbol, "period_end": _date_of_ms(item.get("period_end_ms")), "announce_date": _date_of_ms(item.get("report_date_ms")), "fiscal_year": item.get("fiscal_year"), "report_date_ms": item.get("report_date_ms"), "period_end_ms": item.get("period_end_ms"), **{dst: _number(item.get(src)) for src, dst in CASH_MAP.items()}})
            raw_records.append({"symbol": symbol, "income": income, "indicators": raw_indicators, "cashFlow": cash_items})
        except Exception as exc:
            failures.append({"symbol": symbol, "reason": str(exc)})
        progress(index, len(symbols), f"已处理 {index}/{len(symbols)}，失败 {len(failures)}")
    if metric_rows:
        _merge_frame(DATA_DIR / "financials" / "metrics" / "part.parquet", pl.DataFrame(metric_rows), ["symbol", "period_end"])
    if cash_rows:
        _merge_frame(DATA_DIR / "financials" / "cash_flow" / "part.parquet", pl.DataFrame(cash_rows), ["symbol", "period_end"])
    if raw_records:
        _raw_json("financials", {"source": "hithink", "retrievedAt": now_iso(), "records": raw_records})
    write_json(HITHINK_RAW_DIR / "financials" / "last-failures.json", failures)
    return {"requested": len(symbols), "metricsAdded": len(metric_rows), "cashFlowAdded": len(cash_rows), "failed": len(failures), "message": "仅处理缺失公司；失败原因已留档"}


def _update_market_intelligence(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    requests = [
        ("dragonTiger", "/api/a-share/special-data/dragon-tiger-list", {"board_type": "all"}),
        ("hotStocks", "/api/a-share/special-data/hot-stock-list", {"period": "day"}),
        ("anomalies", "/api/a-share/special-data/anomaly-analysis-list", {}),
    ]
    result: dict[str, Any] = {"source": "hithink", "retrievedAt": now_iso(), "limitations": ["龙虎榜仅覆盖上榜股票", "热榜和异动有数据延迟", "公开资金流指标不等于完整主力持仓"]}
    failures: list[dict[str, str]] = []
    for index, (name, path, params) in enumerate(requests, 1):
        try:
            result[name] = _api_get(client, path, params)
        except Exception as exc:
            failures.append({"dataset": name, "reason": str(exc)})
        progress(index, len(requests), f"已获取 {index}/{len(requests)} 类")
    result["failures"] = failures
    target = _raw_json("market-intelligence", result)
    write_json(NEWS_DIR / "market-intelligence-latest.json", result)
    return {"datasets": len(requests) - len(failures), "failed": len(failures), "rawPath": str(target.relative_to(DATA_DIR)), "message": "公开情绪与资金动向代理数据已更新"}


def _update_research_news(client: httpx.Client, progress: Progress) -> dict[str, Any]:
    result = sync_cninfo(client, progress=progress)
    return {**result, "message": f"巨潮增量同步完成：发现{result['discovered']}条，PDF成功{result['hashed']}条，累计{result['events']}条"}


UPDATERS = {"instruments": _update_instruments, "kline_daily": _update_daily, "adj_factor": _update_adjustment_events, "financials": _update_financials, "valuations_current": _update_current_valuations, "valuations_history": _update_historical_valuations, "ext_data": _update_ext_data, "kline_index_daily": _update_index_daily, "market_intelligence": _update_market_intelligence, "user_data_research_news": _update_research_news}


def _requires_hithink(key: str) -> bool:
    return DATA_SOURCES[key].get("credential", "hithink") == "hithink"


def run_update(key: str) -> None:
    token = load_hithink_key()
    if _requires_hithink(key) and not token:
        _set_job(key, state="failed", finishedAt=now_iso(), message="请先配置HiThink Financial API Key")
        return
    def progress(done: int, total: int | None, message: str) -> None:
        _set_job(key, state="running", done=done, total=total, message=message, updatedAt=now_iso())
    try:
        headers = {"X-api-key": token} if _requires_hithink(key) else (CNINFO_HEADERS if key == "user_data_research_news" else {})
        with httpx.Client(headers=headers, timeout=120, follow_redirects=True) as client:
            result = UPDATERS[key](client, progress)
        invalidate_coverage_cache()
        _set_job(key, state="completed", finishedAt=now_iso(), message=result.get("message"), result=result)
    except Exception as exc:
        _set_job(key, state="failed", finishedAt=now_iso(), message=str(exc))


def start_update(key: str) -> dict[str, Any]:
    source = DATA_SOURCES.get(key)
    if not source:
        raise ValueError("未知数据项")
    if not source["updateSupported"] or key not in UPDATERS:
        raise ValueError(source["method"])
    if _requires_hithink(key) and not load_hithink_key():
        raise ValueError("请先配置HiThink Financial API Key")
    current = _status().get(key, {})
    if current.get("state") in {"queued", "running"}:
        raise ValueError("该数据项正在更新")
    job_id = str(uuid.uuid4())
    _set_job(key, id=job_id, state="queued", startedAt=now_iso(), done=0, total=None, message="等待后台执行", result=None)
    threading.Thread(target=run_update, args=(key,), daemon=True, name=f"data-update-{key}").start()
    return {"key": key, "jobId": job_id, "state": "queued"}
