from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from .config import DATA_DIR, LEAGUE_DIR, NEWS_DIR
from .storage import GROUP_IDS, now_iso, read_json, write_json


DEFAULT_SELECTION_PARAMETERS: dict[str, dict[str, float]] = {
    "S-A": {"min_avg_amount20": 50_000_000, "min_return20": 0, "momentum_weight": 55, "liquidity_weight": 30, "low_vol_weight": 15},
    "S-B": {"min_avg_amount20": 50_000_000, "max_volatility60": .75, "reversal_weight": 50, "low_vol_weight": 30, "liquidity_weight": 20},
    "S-C": {"min_avg_amount20": 50_000_000, "event_weight": 70, "liquidity_weight": 30},
    "S-D": {"min_avg_amount20": 50_000_000, "momentum_weight": 45, "low_vol_weight": 30, "liquidity_weight": 25},
    "L-A": {"min_avg_amount20": 50_000_000, "value_weight": 40, "quality_weight": 30, "low_vol_weight": 20, "liquidity_weight": 10},
    "L-B": {"min_avg_amount20": 50_000_000, "min_return20": -.05, "low_vol_weight": 50, "dividend_weight": 35, "liquidity_weight": 15},
    "L-C": {"min_avg_amount20": 50_000_000, "roe_delta_weight": 45, "roe_weight": 35, "liquidity_weight": 20},
    "L-D": {"min_avg_amount20": 50_000_000, "roe_weight": 30, "low_debt_weight": 20, "low_vol_weight": 20, "momentum_weight": 15, "liquidity_weight": 15},
}


def _active_strategy_overrides() -> dict[str, dict[str, Any]]:
    result = {}
    for group_id in GROUP_IDS:
        root = LEAGUE_DIR / "groups" / group_id
        version = int(read_json(root / "config.json", {}).get("strategy_version", 1))
        result[group_id] = read_json(root / "strategy_versions" / f"v{version}.json", {})
    return result


def _partition_date(path: Path) -> str:
    return path.parent.name.removeprefix("date=")


def _latest_available_date(as_of: str) -> str:
    dates = sorted(
        _partition_date(path)
        for path in (DATA_DIR / "kline_daily_enriched").glob("date=*/part.parquet")
        if _partition_date(path) <= as_of
    )
    if not dates:
        raise ValueError(f"{as_of}及以前没有复权日K")
    return dates[-1]


def _financial_snapshot(as_of: str) -> pl.DataFrame:
    metrics_path = DATA_DIR / "financials" / "metrics" / "part.parquet"
    cash_path = DATA_DIR / "financials" / "cash_flow" / "part.parquet"
    if not metrics_path.exists() or not cash_path.exists():
        return pl.DataFrame({"symbol": []}, schema={"symbol": pl.String})

    metrics = (
        pl.read_parquet(metrics_path)
        .filter(pl.col("announce_date").is_not_null() & (pl.col("announce_date") < as_of))
        .sort(["symbol", "period_end"])
        .with_columns(pl.col("roe").shift(1).over("symbol").alias("roe_previous"))
        .group_by("symbol", maintain_order=True)
        .tail(1)
        .select(
            "symbol", "announce_date", "period_end", "roe", "roe_previous",
            "debt_to_asset_ratio", "revenue_yoy", "net_income_yoy", "net_margin",
        )
    )
    cash = (
        pl.read_parquet(cash_path)
        .filter(pl.col("announce_date").is_not_null() & (pl.col("announce_date") < as_of))
        .sort(["symbol", "period_end"])
        .group_by("symbol", maintain_order=True)
        .tail(1)
        .select("symbol", pl.col("net_operating_cash_flow").alias("operating_cash_flow"))
    )
    return metrics.join(cash, on="symbol", how="left")


def _dividend_snapshot(as_of: str) -> pl.DataFrame:
    path = DATA_DIR / "adj_factor" / "events.parquet"
    empty = pl.DataFrame({
        "symbol": pl.Series([], dtype=pl.String),
        "dividend_ttm": pl.Series([], dtype=pl.Float64),
        "dividend_events_ttm": pl.Series([], dtype=pl.UInt32),
    })
    if not path.exists():
        return empty
    events = pl.read_parquet(path)
    required = {"symbol", "ex_date", "dividend_per_share"}
    if not required.issubset(events.columns):
        return empty
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=365)
    return (
        events.with_columns(pl.col("ex_date").cast(pl.Date, strict=False))
        .filter(
            pl.col("ex_date").is_between(start, end, closed="both")
            & (pl.col("dividend_per_share") > 0)
        )
        .group_by("symbol")
        .agg(
            pl.col("dividend_per_share").sum().alias("dividend_ttm"),
            pl.len().cast(pl.UInt32).alias("dividend_events_ttm"),
        )
    )


def _empty_valuations() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": pl.Series([], dtype=pl.String),
        "pe_ttm": pl.Series([], dtype=pl.Float64),
        "valuation_date": pl.Series([], dtype=pl.Date),
        "valuation_source": pl.Series([], dtype=pl.String),
        "valuation_is_st": pl.Series([], dtype=pl.String),
    })


def _valuation_snapshot(as_of: str, trade_date: str) -> pl.DataFrame:
    if as_of == date.today().isoformat():
        current_path = DATA_DIR / "valuations" / "current" / "part.parquet"
        if current_path.exists():
            current = pl.read_parquet(current_path)
            if {"symbol", "pe_ttm", "valuation_date", "source"}.issubset(current.columns):
                return (
                    current.with_columns(pl.col("valuation_date").cast(pl.Date, strict=False))
                    .filter(pl.col("valuation_date") == date.fromisoformat(as_of))
                    .select("symbol", "pe_ttm", "valuation_date", pl.col("source").alias("valuation_source"), pl.lit("0").alias("valuation_is_st"))
                    .unique("symbol", keep="last")
                )
    paths = sorted((DATA_DIR / "valuations" / "history").glob("batch-*.parquet"))
    if not paths:
        return _empty_valuations()
    history = pl.concat([pl.read_parquet(path, columns=["symbol", "date", "pe_ttm", "source", "is_st"]) for path in paths], how="vertical_relaxed")
    return (
        history.filter(pl.col("date") == date.fromisoformat(trade_date))
        .select("symbol", "pe_ttm", pl.col("date").alias("valuation_date"), pl.col("source").alias("valuation_source"), pl.col("is_st").alias("valuation_is_st"))
        .unique("symbol", keep="last")
    )


def _factor_snapshot(as_of: str) -> tuple[str, pl.DataFrame]:
    trade_date = _latest_available_date(as_of)
    paths = sorted(
        path for path in (DATA_DIR / "kline_daily_enriched").glob("date=*/part.parquet")
        if _partition_date(path) <= trade_date
    )[-130:]
    history = pl.concat(
        [pl.read_parquet(path, columns=["symbol", "date", "close", "amount", "consecutive_limit_ups", "consecutive_limit_downs"]) for path in paths],
        how="vertical_relaxed",
    ).sort(["symbol", "date"])
    history = history.with_columns(
        pl.len().over("symbol").alias("history_count"),
        pl.col("close").rolling_mean(20).over("symbol").alias("ma20"),
        pl.col("close").rolling_mean(60).over("symbol").alias("ma60"),
        pl.col("close").pct_change(5).over("symbol").alias("return5"),
        pl.col("close").pct_change(20).over("symbol").alias("return20"),
        pl.col("amount").rolling_mean(20).over("symbol").alias("avg_amount20"),
        pl.col("close").pct_change().over("symbol").alias("daily_return"),
    ).with_columns(
        (pl.col("daily_return").rolling_std(60).over("symbol") * math.sqrt(252)).alias("volatility60")
    )
    latest = history.group_by("symbol", maintain_order=True).tail(1)
    instruments = pl.read_parquet(DATA_DIR / "instruments" / "instruments.parquet", columns=["symbol", "name", "code"])
    latest = latest.join(instruments, on="symbol", how="inner").filter(
        ~pl.col("name").str.to_uppercase().str.contains(r"ST|退")
        & ~pl.col("code").str.starts_with("688")
        & ~pl.col("code").str.starts_with("689")
        & ~pl.col("code").str.starts_with("300")
        & ~pl.col("code").str.starts_with("301")
        & ~pl.col("symbol").str.ends_with(".BJ")
        & (pl.col("history_count") >= 60)
        & (pl.col("consecutive_limit_ups") == 0)
        & (pl.col("consecutive_limit_downs") == 0)
    )
    latest = latest.join(_financial_snapshot(as_of), on="symbol", how="left")
    latest = latest.join(_valuation_snapshot(as_of, trade_date), on="symbol", how="left")
    return trade_date, latest.join(_dividend_snapshot(as_of), on="symbol", how="left").with_columns(
        pl.when((pl.col("dividend_ttm") > 0) & (pl.col("close") > 0))
        .then(pl.col("dividend_ttm") / pl.col("close"))
        .otherwise(None)
        .alias("dividend_yield")
    )


def _percentile(frame: pl.DataFrame, column: str, *, descending: bool = False) -> pl.Expr:
    return (pl.col(column).rank(method="average", descending=descending) / max(frame.height, 1)).alias(f"{column}_rank")


def _items(frame: pl.DataFrame, score: pl.Expr, *, entry_min: float, entry_max: float, reasons: list[str] | None = None) -> list[dict[str, Any]]:
    if frame.is_empty():
        return []
    ranked = frame.with_columns(score.alias("score")).sort("score", descending=True).head(5)
    rows = []
    for row in ranked.to_dicts():
        close = float(row["close"])
        rows.append({
            "symbol": row["symbol"], "name": row["name"], "close": round(close, 4),
            "score": round(float(row["score"]), 4),
            "return5": row.get("return5"), "return20": row.get("return20"),
            "volatility60": row.get("volatility60"), "avg_amount20": row.get("avg_amount20"),
            "roe": row.get("roe"), "roe_change": (row.get("roe") - row.get("roe_previous")) if row.get("roe") is not None and row.get("roe_previous") is not None else None,
            "operating_cash_flow": row.get("operating_cash_flow"),
            "pe_ttm": row.get("pe_ttm"),
            "valuation_date": row["valuation_date"].isoformat() if hasattr(row.get("valuation_date"), "isoformat") else row.get("valuation_date"),
            "valuation_source": row.get("valuation_source"),
            "dividend_ttm": row.get("dividend_ttm"), "dividend_events_ttm": row.get("dividend_events_ttm"),
            "dividend_yield": row.get("dividend_yield"), "selection_reason": reasons or [],
            "entry_reference_price": close,
            "entry_price_min": round(close * entry_min, 4), "entry_price_max": round(close * entry_max, 4),
        })
    return rows


def _select_groups(frame: pl.DataFrame, as_of: str, strategies: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    def params(group_id: str) -> dict[str, float]:
        override = (((strategies or {}).get(group_id) or {}).get("parameters") or {}).get("selection") or {}
        return {**DEFAULT_SELECTION_PARAMETERS[group_id], **override}

    result: dict[str, dict[str, Any]] = {}

    p = params("S-A")
    common = frame.filter((pl.col("avg_amount20") >= p["min_avg_amount20"]) & pl.col("volatility60").is_not_null())
    sa = common.filter((pl.col("ma20") > pl.col("ma60")) & (pl.col("return20") > p["min_return20"]))
    sa = sa.with_columns(_percentile(sa, "return20"), _percentile(sa, "avg_amount20"), _percentile(sa, "volatility60", descending=True))
    result["S-A"] = {"items": _items(sa, pl.col("return20_rank") * p["momentum_weight"] + pl.col("avg_amount20_rank") * p["liquidity_weight"] + pl.col("volatility60_rank") * p["low_vol_weight"], entry_min=.985, entry_max=1.02), "notes": ["MA20>MA60", f"20日收益>{p['min_return20']:.2%}", f"20日成交额不少于{p['min_avg_amount20'] / 10_000:.0f}万元"]}

    p = params("S-B")
    common = frame.filter((pl.col("avg_amount20") >= p["min_avg_amount20"]) & pl.col("volatility60").is_not_null())
    sb = common.filter((pl.col("return5") < 0) & (pl.col("close") >= pl.col("ma60")) & (pl.col("volatility60") <= p["max_volatility60"]))
    sb = sb.with_columns(_percentile(sb, "return5", descending=True), _percentile(sb, "volatility60", descending=True), _percentile(sb, "avg_amount20"))
    result["S-B"] = {"items": _items(sb, pl.col("return5_rank") * p["reversal_weight"] + pl.col("volatility60_rank") * p["low_vol_weight"] + pl.col("avg_amount20_rank") * p["liquidity_weight"], entry_min=.96, entry_max=1.0), "notes": ["5日收益为负", "价格不低于MA60", f"60日波动率不高于{p['max_volatility60']:.0%}"]}

    p = params("S-C")
    common = frame.filter((pl.col("avg_amount20") >= p["min_avg_amount20"]) & pl.col("volatility60").is_not_null())
    events = read_json(NEWS_DIR / "events.json", [])
    event_scores: dict[str, float] = {}
    for event in events:
        if event.get("actionable") and str(event.get("published_at", ""))[:10] < as_of and event.get("source") == "cninfo":
            event_scores[event["symbol"]] = max(event_scores.get(event["symbol"], 0), float(event.get("signal_score") or 0))
    if event_scores:
        event_frame = pl.DataFrame({"symbol": list(event_scores), "event_score": list(event_scores.values())})
        sc = common.join(event_frame, on="symbol", how="inner").filter(pl.col("event_score") > 0)
        sc = sc.with_columns(_percentile(sc, "event_score"), _percentile(sc, "avg_amount20"))
        sc_items = _items(sc, pl.col("event_score_rank") * p["event_weight"] + pl.col("avg_amount20_rank") * p["liquidity_weight"], entry_min=.985, entry_max=1.035)
    else:
        sc_items = []
    result["S-C"] = {"items": sc_items, "notes": ["仅使用截止日前已发布的巨潮公告", "未使用社交情绪单独触发"]}

    p = params("S-D")
    common = frame.filter((pl.col("avg_amount20") >= p["min_avg_amount20"]) & pl.col("volatility60").is_not_null())
    sd = common.filter(pl.col("return20").is_not_null())
    sd = sd.with_columns(_percentile(sd, "return20"), _percentile(sd, "volatility60", descending=True), _percentile(sd, "avg_amount20"))
    result["S-D"] = {"items": _items(sd, pl.col("return20_rank") * p["momentum_weight"] + pl.col("volatility60_rank") * p["low_vol_weight"] + pl.col("avg_amount20_rank") * p["liquidity_weight"], entry_min=.985, entry_max=1.015), "notes": ["动量、低波、流动性横截面排名"]}

    p = params("L-B")
    common = frame.filter((pl.col("avg_amount20") >= p["min_avg_amount20"]) & pl.col("volatility60").is_not_null())
    if {"dividend_ttm", "dividend_yield"}.issubset(common.columns):
        lb = common.filter(
            (pl.col("dividend_ttm") > 0)
            & pl.col("dividend_yield").is_not_null()
            & (pl.col("close") > pl.col("ma60"))
            & (pl.col("return20") > p["min_return20"])
        )
        lb = lb.with_columns(
            _percentile(lb, "volatility60", descending=True),
            _percentile(lb, "dividend_yield"),
            _percentile(lb, "avg_amount20"),
        )
        lb_items = _items(
            lb,
            pl.col("volatility60_rank") * p["low_vol_weight"]
            + pl.col("dividend_yield_rank") * p["dividend_weight"]
            + pl.col("avg_amount20_rank") * p["liquidity_weight"],
            entry_min=.97,
            entry_max=1.0,
            reasons=["60日低波", "近12个月已实施现金分红", "流动性达标"],
        )
    else:
        lb_items = []
    result["L-B"] = {
        "items": lb_items,
        "status": "ready" if lb_items else "blocked",
        "notes": [f"{lb.height}只股票通过低波、趋势和近12个月已实施分红门禁", "未来除息事件未计入"] if lb_items else ["没有股票同时具备近12个月已实施现金分红和完整低波数据"],
    }

    p = params("L-A")
    common = frame.filter((pl.col("avg_amount20") >= p["min_avg_amount20"]) & pl.col("volatility60").is_not_null())
    financial = common.filter(pl.col("roe").is_not_null() & pl.col("operating_cash_flow").is_not_null())
    la = financial.filter(
        (pl.col("roe") > 0) & (pl.col("operating_cash_flow") > 0)
        & pl.col("pe_ttm").is_not_null() & (pl.col("pe_ttm") > 0)
        & (pl.col("valuation_is_st") == "0")
        & (pl.col("close") > pl.col("ma60"))
    )
    la = la.with_columns(
        _percentile(la, "pe_ttm", descending=True), _percentile(la, "roe"),
        _percentile(la, "volatility60", descending=True), _percentile(la, "avg_amount20"),
    )
    la_items = _items(
        la,
        pl.col("pe_ttm_rank") * p["value_weight"] + pl.col("roe_rank") * p["quality_weight"]
        + pl.col("volatility60_rank") * p["low_vol_weight"] + pl.col("avg_amount20_rank") * p["liquidity_weight"],
        entry_min=.97, entry_max=1.0,
        reasons=["PE TTM为正并按低估值排名", "PIT ROE和经营现金流为正", "价格高于MA60"],
    )
    valuation_evidence = sorted({
        f"{item.get('valuation_source')} {item.get('valuation_date')}"
        for item in la_items if item.get("valuation_source") and item.get("valuation_date")
    })
    valuation_note = "估值来源：" + "、".join(valuation_evidence) if valuation_evidence else "没有可核验的估值来源"
    result["L-A"] = {
        "items": la_items, "status": "ready" if la_items else "blocked",
        "notes": [f"{la.height}家公司通过ROE、现金流、正PE TTM与趋势门禁", "估值缺失或非正值已排除", valuation_note] if la_items else [f"{financial.height}家公司具备ROE和现金流", "没有公司同时具备对应决策日的正PE TTM与趋势条件"],
    }

    p = params("L-C")
    lc = financial.filter((pl.col("roe") > pl.col("roe_previous")) & (pl.col("operating_cash_flow") > 0))
    lc = lc.with_columns((pl.col("roe") - pl.col("roe_previous")).alias("roe_delta"))
    lc = lc.with_columns(_percentile(lc, "roe_delta"), _percentile(lc, "roe"), _percentile(lc, "avg_amount20"))
    result["L-C"] = {"items": _items(lc, pl.col("roe_delta_rank") * p["roe_delta_weight"] + pl.col("roe_rank") * p["roe_weight"] + pl.col("avg_amount20_rank") * p["liquidity_weight"], entry_min=.975, entry_max=1.01), "notes": ["最新可见ROE高于前一期", "经营现金流为正", "公告日期严格早于数据截止日"]}

    p = params("L-D")
    common = frame.filter((pl.col("avg_amount20") >= p["min_avg_amount20"]) & pl.col("volatility60").is_not_null())
    financial = common.filter(pl.col("roe").is_not_null() & pl.col("operating_cash_flow").is_not_null())
    ld = financial.filter(pl.col("debt_to_asset_ratio").is_not_null() & pl.col("return20").is_not_null())
    ld = ld.with_columns(_percentile(ld, "roe"), _percentile(ld, "debt_to_asset_ratio", descending=True), _percentile(ld, "volatility60", descending=True), _percentile(ld, "return20"), _percentile(ld, "avg_amount20"))
    result["L-D"] = {"items": _items(ld, pl.col("roe_rank") * p["roe_weight"] + pl.col("debt_to_asset_ratio_rank") * p["low_debt_weight"] + pl.col("volatility60_rank") * p["low_vol_weight"] + pl.col("return20_rank") * p["momentum_weight"] + pl.col("avg_amount20_rank") * p["liquidity_weight"], entry_min=.97, entry_max=1.0), "notes": ["缺失因子不填零", "质量、低负债、低波、动量与流动性排名"]}
    return result


def run_selection(as_of: str | None = None) -> dict[str, Any]:
    requested = as_of or date.today().isoformat()
    trade_date, factors = _factor_snapshot(requested)
    selections = _select_groups(factors, requested, _active_strategy_overrides())
    generated_at = now_iso()
    summaries = []
    for group_id in GROUP_IDS:
        group_dir = LEAGUE_DIR / "groups" / group_id
        config = read_json(group_dir / "config.json", {})
        selected = selections[group_id]
        snapshot = {
            "generated_at": generated_at,
            "as_of": requested,
            "trade_date": trade_date,
            "status": selected.get("status", "ready"),
            "strategy_version": config.get("strategy_version"),
            "notes": selected["notes"],
            "items": selected["items"],
        }
        write_json(group_dir / "candidates.json", snapshot)
        summaries.append({"groupId": group_id, "status": snapshot["status"], "count": len(snapshot["items"]), "notes": snapshot["notes"]})
    run = {"generatedAt": generated_at, "asOf": requested, "tradeDate": trade_date, "eligibleUniverse": factors.height, "groups": summaries}
    write_json(LEAGUE_DIR / "latest_selection_run.json", run)
    return run
