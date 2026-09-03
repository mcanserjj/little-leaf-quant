from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .config import DATA_DIR, LEAGUE_DIR, NEWS_DIR

GROUP_IDS = ("S-A", "S-B", "S-C", "S-D", "L-A", "L-B", "L-C", "L-D")
_coverage_cache: tuple[float, list[dict[str, Any]]] | None = None


def invalidate_coverage_cache() -> None:
    global _coverage_cache
    _coverage_cache = None


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _latest_strategy(group_dir: Path, active_version: int | None = None) -> dict[str, Any]:
    if active_version is not None:
        active = read_json(group_dir / "strategy_versions" / f"v{active_version}.json", {})
        if active:
            return active
    versions = []
    for path in (group_dir / "strategy_versions").glob("v*.json"):
        item = read_json(path, {})
        if item:
            versions.append(item)
    versions.sort(key=lambda item: int(item.get("version", 0)), reverse=True)
    return versions[0] if versions else {}


def group_snapshot(group_id: str) -> dict[str, Any]:
    group_dir = LEAGUE_DIR / "groups" / group_id
    account = read_json(group_dir / "account.json", {})
    positions = read_json(group_dir / "positions.json", [])
    trades = read_json(group_dir / "trades.json", [])
    orders = read_json(group_dir / "orders.json", [])
    decisions = read_json(group_dir / "decisions.json", [])
    config = read_json(group_dir / "config.json", {"group_id": group_id})
    candidates = read_json(group_dir / "candidates.json", {})
    strategy = _latest_strategy(group_dir, int(config.get("strategy_version", 1)))
    initial = float(account.get("initial_cash") or 100000)
    nav = float(account.get("nav") or initial)
    return {
        "id": group_id,
        "name": config.get("name", group_id),
        "horizon": config.get("horizon", "short" if group_id.startswith("S") else "long"),
        "status": config.get("status", "unknown"),
        "returnPct": round((nav / initial - 1) * 100, 4) if initial else 0,
        "account": account,
        "positions": positions,
        "trades": trades,
        "orders": orders[-20:],
        "decisions": decisions[-20:],
        "candidates": candidates,
        "strategy": strategy,
    }


def all_groups() -> list[dict[str, Any]]:
    return [group_snapshot(group_id) for group_id in GROUP_IDS]


def data_coverage() -> list[dict[str, Any]]:
    global _coverage_cache
    if _coverage_cache and time.monotonic() - _coverage_cache[0] < 30:
        return _coverage_cache[1]
    instrument_path = DATA_DIR / "instruments" / "instruments.parquet"
    instruments = pl.read_parquet(instrument_path, columns=["symbol", "name"]) if instrument_path.exists() else pl.DataFrame({"symbol": [], "name": []}, schema={"symbol": pl.String, "name": pl.String})
    instrument_symbols = set(instruments["symbol"].to_list())
    research_symbols = set(instruments.filter(
        ~pl.col("name").str.to_uppercase().str.contains(r"ST|退")
        & ~pl.col("symbol").str.starts_with("688") & ~pl.col("symbol").str.starts_with("689")
        & ~pl.col("symbol").str.starts_with("300") & ~pl.col("symbol").str.starts_with("301")
        & ~pl.col("symbol").str.ends_with(".BJ")
    )["symbol"].to_list())
    total_companies = len(instrument_symbols)
    definitions = (
        ("股票列表", "instruments"),
        ("日K", "kline_daily"),
        ("复权日K", "kline_daily_enriched"),
        ("除权因子", "adj_factor"),
        ("财务数据", "financials"),
        ("当前估值", "valuations/current"),
        ("历史估值", "valuations/history"),
        ("扩展数据", "ext_data"),
        ("指数日K", "kline_index_daily"),
        ("场外资讯", "user_data/research_news"),
    )
    result: list[dict[str, Any]] = []
    for label, relative in definitions:
        root = DATA_DIR / relative
        files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
        size = sum(path.stat().st_size for path in files)
        latest = max((path.stat().st_mtime for path in files), default=None)
        complete_companies: int | None = None
        coverage_total = total_companies
        if files and relative in {"instruments", "adj_factor"}:
            frames = [pl.read_parquet(path, columns=["symbol"]) for path in files if path.suffix == ".parquet"]
            complete_companies = len(set(pl.concat(frames)["symbol"].to_list()) & instrument_symbols) if frames else 0
        elif files and relative in {"kline_daily", "kline_daily_enriched"}:
            latest_file = max((path for path in files if path.name == "part.parquet"), key=lambda path: path.parent.name, default=None)
            complete_companies = len(set(pl.read_parquet(latest_file, columns=["symbol"])["symbol"].to_list()) & instrument_symbols) if latest_file else 0
        elif relative == "financials":
            metrics_path = root / "metrics" / "part.parquet"
            cash_path = root / "cash_flow" / "part.parquet"
            if metrics_path.exists() and cash_path.exists():
                metric_symbols = set(pl.read_parquet(metrics_path, columns=["symbol", "period_end", "roe"]).filter(pl.col("roe").is_not_null()).group_by("symbol").agg(pl.col("period_end").n_unique().alias("periods")).filter(pl.col("periods") >= 2)["symbol"].to_list())
                cash_symbols = set(pl.read_parquet(cash_path, columns=["symbol", "net_operating_cash_flow"]).filter(pl.col("net_operating_cash_flow").is_not_null())["symbol"].to_list())
                complete_companies = len(metric_symbols & cash_symbols & research_symbols)
                coverage_total = len(research_symbols)
        elif relative == "valuations/current":
            current_path = root / "part.parquet"
            if current_path.exists():
                current = pl.read_parquet(current_path, columns=["symbol", "pe_ttm"])
                complete_companies = len(set(current.filter(pl.col("pe_ttm").is_not_null() & (pl.col("pe_ttm") > 0))["symbol"].to_list()) & research_symbols)
                coverage_total = len(research_symbols)
        elif relative == "valuations/history":
            covered = read_json(root / "coverage.json", {})
            complete_companies = len(set(covered) & research_symbols)
            coverage_total = len(research_symbols)
        elif relative == "user_data/research_news":
            events = read_json(NEWS_DIR / "events.json", [])
            complete_companies = len({event.get("symbol") for event in events if event.get("symbol")})
        result.append({
            "key": relative.replace("/", "_"),
            "label": label,
            "files": len(files),
            "bytes": size,
            "updatedAt": datetime.fromtimestamp(latest, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds") if latest else None,
            "available": bool(files),
            "completeCompanies": complete_companies,
            "totalCompanies": coverage_total if complete_companies is not None else None,
            "scope": "选股研究池（排除ST、退市、科创、创业板、北交所）" if relative in {"financials", "valuations/current", "valuations/history"} else "全部本地标的",
        })
    _coverage_cache = (time.monotonic(), result)
    return result


def overview() -> dict[str, Any]:
    groups = all_groups()
    state = read_json(LEAGUE_DIR / "execution_state.json", {})
    news = read_json(NEWS_DIR / "status.json", {})
    return {
        "updatedAt": now_iso(),
        "lastQuoteAt": state.get("last_cycle_at"),
        "quoteCount": state.get("quote_count", 0),
        "groups": len(groups),
        "positions": sum(len(group["positions"]) for group in groups),
        "averageReturnPct": round(sum(group["returnPct"] for group in groups) / len(groups), 4),
        "news": news,
        "coverage": data_coverage(),
    }
