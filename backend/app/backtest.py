from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from .config import DATA_DIR, LEAGUE_DIR, RUNTIME_DIR
from .selection import _select_groups
from .storage import GROUP_IDS, now_iso, read_json, write_json


STATUS_PATH = RUNTIME_DIR / "backtest.json"
MODES = ("current_snapshot", "next_snapshot")
SLIPPAGE_CASES = (0, 5, 10)


@dataclass
class Position:
    symbol: str
    quantity: int
    buy_date: str
    buy_price: float
    buy_fee: float
    trading_days: int = 0


def _fees(value: float, side: str, rules: dict[str, Any]) -> float:
    fee_rules = rules["fees"]
    commission = max(value * float(fee_rules["broker_commission_rate"]), float(fee_rules["broker_commission_min"]))
    transfer = value * float(fee_rules["transfer_fee_rate"])
    stamp = value * float(fee_rules["stamp_duty_sell_rate"]) if side == "sell" else 0.0
    return commission + transfer + stamp


def _market_history() -> pl.DataFrame:
    paths = sorted((DATA_DIR / "kline_daily_enriched").glob("date=*/part.parquet"))
    if not paths:
        raise ValueError("没有复权日K，不能回测")
    columns = ["symbol", "date", "open", "high", "low", "close", "raw_close", "amount", "consecutive_limit_ups", "consecutive_limit_downs"]
    frame = pl.concat([pl.read_parquet(path, columns=columns) for path in paths], how="vertical_relaxed").sort(["symbol", "date"])
    frame = frame.with_columns(
        pl.len().over("symbol").alias("history_count"),
        pl.col("close").rolling_mean(20).over("symbol").alias("ma20"),
        pl.col("close").rolling_mean(60).over("symbol").alias("ma60"),
        pl.col("close").pct_change(5).over("symbol").alias("return5"),
        pl.col("close").pct_change(20).over("symbol").alias("return20"),
        pl.col("amount").rolling_mean(20).over("symbol").alias("avg_amount20"),
        pl.col("close").pct_change().over("symbol").alias("daily_return"),
    ).with_columns((pl.col("daily_return").rolling_std(60).over("symbol") * math.sqrt(252)).alias("volatility60"))

    instruments = pl.read_parquet(DATA_DIR / "instruments" / "instruments.parquet", columns=["symbol", "name", "code"])
    frame = frame.join(instruments, on="symbol", how="inner").filter(
        ~pl.col("name").str.to_uppercase().str.contains(r"ST|退")
        & ~pl.col("code").str.starts_with("688")
        & ~pl.col("code").str.starts_with("689")
        & ~pl.col("code").str.starts_with("300")
        & ~pl.col("code").str.starts_with("301")
        & ~pl.col("symbol").str.ends_with(".BJ")
    )
    return _join_historical_valuations(_join_trailing_dividends(_join_pit_financials(frame)))


def _join_pit_financials(market: pl.DataFrame) -> pl.DataFrame:
    metrics_path = DATA_DIR / "financials" / "metrics" / "part.parquet"
    cash_path = DATA_DIR / "financials" / "cash_flow" / "part.parquet"
    if not metrics_path.exists() or not cash_path.exists():
        return market.with_columns(*[pl.lit(None).cast(pl.Float64).alias(name) for name in (
            "roe", "roe_previous", "debt_to_asset_ratio", "revenue_yoy", "net_income_yoy", "net_margin", "operating_cash_flow",
        )])
    metrics = (
        pl.read_parquet(metrics_path)
        .filter(pl.col("announce_date").is_not_null())
        .with_columns(pl.col("announce_date").str.to_date(strict=False))
        .sort(["symbol", "announce_date", "period_end"])
        .with_columns(pl.col("roe").shift(1).over("symbol").alias("roe_previous"))
        .select("symbol", "announce_date", "roe", "roe_previous", "debt_to_asset_ratio", "revenue_yoy", "net_income_yoy", "net_margin")
    )
    cash = (
        pl.read_parquet(cash_path)
        .filter(pl.col("announce_date").is_not_null())
        .with_columns(pl.col("announce_date").str.to_date(strict=False))
        .sort(["symbol", "announce_date", "period_end"])
        .select("symbol", "announce_date", pl.col("net_operating_cash_flow").alias("operating_cash_flow"))
    )
    # backward + allow_exact_matches=False enforces publication time strictly before signal date.
    market = market.sort(["symbol", "date"]).join_asof(
        metrics.sort(["symbol", "announce_date"]), left_on="date", right_on="announce_date", by="symbol",
        strategy="backward", allow_exact_matches=False,
    ).drop("announce_date")
    return market.sort(["symbol", "date"]).join_asof(
        cash.sort(["symbol", "announce_date"]), left_on="date", right_on="announce_date", by="symbol",
        strategy="backward", allow_exact_matches=False,
    ).drop("announce_date")


def _join_trailing_dividends(market: pl.DataFrame) -> pl.DataFrame:
    path = DATA_DIR / "adj_factor" / "events.parquet"
    if not path.exists():
        return market.with_columns(
            pl.lit(None).cast(pl.Float64).alias("dividend_ttm"),
            pl.lit(None).cast(pl.UInt32).alias("dividend_events_ttm"),
            pl.lit(None).cast(pl.Float64).alias("dividend_yield"),
        )
    events = pl.read_parquet(path)
    required = {"symbol", "ex_date", "dividend_per_share"}
    if not required.issubset(events.columns):
        return market.with_columns(
            pl.lit(None).cast(pl.Float64).alias("dividend_ttm"),
            pl.lit(None).cast(pl.UInt32).alias("dividend_events_ttm"),
            pl.lit(None).cast(pl.Float64).alias("dividend_yield"),
        )
    daily = (
        events.with_columns(pl.col("ex_date").cast(pl.Date, strict=False))
        .filter(pl.col("ex_date").is_not_null() & (pl.col("dividend_per_share") > 0))
        .group_by("symbol", pl.col("ex_date").alias("date"))
        .agg(
            pl.col("dividend_per_share").sum().alias("cash_dividend"),
            pl.len().cast(pl.UInt32).alias("cash_dividend_events"),
        )
    )
    price_column = "raw_close" if "raw_close" in market.columns else "close"
    return (
        market.join(daily, on=["symbol", "date"], how="left")
        .sort(["symbol", "date"])
        .with_columns(
            pl.col("cash_dividend").fill_null(0.0)
            .rolling_sum_by("date", window_size="365d", closed="both")
            .over("symbol").alias("dividend_ttm"),
            pl.col("cash_dividend_events").fill_null(0)
            .rolling_sum_by("date", window_size="365d", closed="both")
            .over("symbol").cast(pl.UInt32).alias("dividend_events_ttm"),
        )
        .with_columns(
            pl.when((pl.col("dividend_ttm") > 0) & (pl.col(price_column) > 0))
            .then(pl.col("dividend_ttm") / pl.col(price_column))
            .otherwise(None)
            .alias("dividend_yield")
        )
        .drop("cash_dividend", "cash_dividend_events")
    )


def _join_historical_valuations(market: pl.DataFrame) -> pl.DataFrame:
    paths = sorted((DATA_DIR / "valuations" / "history").glob("batch-*.parquet"))
    if not paths:
        return market.with_columns(
            pl.lit(None).cast(pl.Float64).alias("pe_ttm"),
            pl.lit(None).cast(pl.String).alias("valuation_source"),
            pl.lit(None).cast(pl.String).alias("valuation_is_st"),
        )
    values = (
        pl.concat([pl.read_parquet(path, columns=["symbol", "date", "pe_ttm", "source", "is_st"]) for path in paths], how="vertical_relaxed")
        .unique(["symbol", "date"], keep="last")
        .rename({"source": "valuation_source", "is_st": "valuation_is_st"})
    )
    return market.join(values, on=["symbol", "date"], how="left")


def _entry_band(candidate: dict[str, Any], strategy: dict[str, Any]) -> tuple[float, float]:
    reference = float(candidate["close"])
    entry = (strategy.get("parameters") or {}).get("entry_price") or {}
    if not entry:
        return 0.0, math.inf
    annual_vol = candidate.get("volatility60")
    daily_vol = float(annual_vol) / math.sqrt(252) if annual_vol is not None else float(entry.get("fallback_daily_vol_pct", .02))
    daily_vol = min(max(daily_vol, float(entry.get("min_daily_vol_pct", .008))), float(entry.get("max_daily_vol_pct", .05)))
    low = reference * (1 + daily_vol * float(entry.get("lower_vol_multiplier", -.5)))
    high = reference * (1 + daily_vol * float(entry.get("upper_vol_multiplier", .75)))
    return low, high


def _summary(equity: list[float], trades: list[dict[str, Any]], initial_cash: float) -> dict[str, Any]:
    if not equity:
        return {"totalReturnPct": 0.0, "maxDrawdownPct": 0.0, "sharpe": 0.0, "winRatePct": 0.0, "trades": 0, "turnover": 0.0, "fees": 0.0}
    returns = [(equity[index] / equity[index - 1] - 1) for index in range(1, len(equity)) if equity[index - 1] > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = sum((value - mean) ** 2 for value in returns) / max(len(returns) - 1, 1) if returns else 0.0
    sharpe = mean / math.sqrt(variance) * math.sqrt(252) if variance > 0 else 0.0
    peak = equity[0]
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    exits = [item for item in trades if item["side"] == "sell"]
    wins = sum(1 for item in exits if item["netPnl"] > 0)
    return {
        "totalReturnPct": round((equity[-1] / initial_cash - 1) * 100, 4),
        "maxDrawdownPct": round(drawdown * 100, 4),
        "sharpe": round(sharpe, 4),
        "winRatePct": round(wins / len(exits) * 100, 2) if exits else 0.0,
        "trades": len(exits),
        "turnover": round(sum(item["value"] for item in trades) / initial_cash, 4),
        "fees": round(sum(item["fee"] for item in trades), 2),
    }


def _partition_days(frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return {str(day.item(0, "date")): day for day in frame.partition_by("date", maintain_order=True)}


def _selection_schedule(days: dict[str, pl.DataFrame], strategies: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    dates = sorted(days)
    return {
        trade_date: _select_groups(
            days[trade_date].filter(
                (pl.col("history_count") >= 60)
                & (pl.col("consecutive_limit_ups") == 0)
                & (pl.col("consecutive_limit_downs") == 0)
            ),
            trade_date, strategies,
        )
        for index, trade_date in enumerate(dates)
        if index % 5 == 0 and index + 1 < len(dates)
    }


def simulate(frame: pl.DataFrame, group_id: str, strategy: dict[str, Any], mode: str, slippage_bps: int, *, start: str, end: str, day_frames: dict[str, pl.DataFrame] | None = None, selections: dict[str, dict[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    rules = read_json(LEAGUE_DIR / "rules.json", {})
    initial_cash = float(rules.get("initial_cash", 100000))
    max_positions = int(rules.get("max_positions", 5))
    days = day_frames or _partition_days(frame)
    schedule = selections or _selection_schedule(days, {group_id: strategy})
    dates = [value for value in sorted(days) if start <= value <= end]
    cash = initial_cash
    positions: dict[str, Position] = {}
    pending: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    equity: list[float] = []
    params = strategy.get("parameters") or {}
    stop_loss = float(params.get("stop_loss_pct", -.06))
    take_profit = float(params.get("take_profit_pct", .12))
    max_days = int(params.get("max_holding_days", 10))
    for index, trade_date in enumerate(dates):
        day = days[trade_date]
        rows = {row["symbol"]: row for row in day.to_dicts()}
        # Orders are always generated from the previous signal date, avoiding same-bar look-ahead.
        for order in list(pending):
            if len(positions) >= max_positions or order["symbol"] in positions:
                continue
            row = rows.get(order["symbol"])
            if not row or row.get("consecutive_limit_ups", 0) or row.get("consecutive_limit_downs", 0):
                continue
            raw_price = float(row["open"] if mode == "current_snapshot" else row["close"])
            if not order["low"] <= raw_price <= order["high"]:
                continue
            price = raw_price * (1 + slippage_bps / 10000)
            budget = min(initial_cash / max_positions, cash)
            quantity = int(budget / price / 100) * 100
            value = quantity * price
            fee = _fees(value, "buy", rules)
            if quantity < 100 or value + fee > cash:
                continue
            cash -= value + fee
            positions[order["symbol"]] = Position(order["symbol"], quantity, str(trade_date), price, fee)
            trades.append({"date": str(trade_date), "side": "buy", "symbol": order["symbol"], "price": round(price, 4), "quantity": quantity, "value": round(value, 2), "fee": round(fee, 2), "reason": "entry_band"})
        pending = []

        for symbol, position in list(positions.items()):
            row = rows.get(symbol)
            if not row:
                continue
            position.trading_days += 1
            pnl_low = float(row["low"]) / position.buy_price - 1
            pnl_high = float(row["high"]) / position.buy_price - 1
            reason = ""
            raw_price = float(row["close"])
            if position.trading_days > 1 and pnl_low <= stop_loss:
                reason, raw_price = "stop_loss", min(float(row["open"]), position.buy_price * (1 + stop_loss))
            elif position.trading_days > 1 and pnl_high >= take_profit:
                reason, raw_price = "take_profit", max(float(row["open"]), position.buy_price * (1 + take_profit))
            elif position.trading_days >= max_days:
                reason = "max_holding_days"
            if not reason or position.trading_days <= 1 or row.get("consecutive_limit_downs", 0):
                continue
            price = raw_price * (1 - slippage_bps / 10000)
            value = position.quantity * price
            fee = _fees(value, "sell", rules)
            cash += value - fee
            net_pnl = value - fee - position.quantity * position.buy_price - position.buy_fee
            trades.append({"date": str(trade_date), "side": "sell", "symbol": symbol, "price": round(price, 4), "quantity": position.quantity, "value": round(value, 2), "fee": round(fee, 2), "netPnl": round(net_pnl, 2), "reason": reason})
            del positions[symbol]

        if trade_date in schedule and index + 1 < len(dates):
            selected = schedule[trade_date][group_id]
            for item in selected["items"]:
                low, high = _entry_band(item, strategy)
                pending.append({"symbol": item["symbol"], "low": low, "high": high})

        market_value = sum(position.quantity * float(rows.get(symbol, {}).get("close", position.buy_price)) for symbol, position in positions.items())
        equity.append(cash + market_value)
    result = _summary(equity, trades, initial_cash)
    result.update({"eligibleData": True, "start": start, "end": end, "mode": mode, "slippageBps": slippage_bps, "openPositions": len(positions), "orders": len([item for item in trades if item["side"] == "buy"])})
    return result


def evaluate_version(group_id: str, version: int, frame: pl.DataFrame | None = None, day_frames: dict[str, pl.DataFrame] | None = None, selections: dict[str, dict[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    root = LEAGUE_DIR / "groups" / group_id
    strategy = read_json(root / "strategy_versions" / f"v{version}.json", {})
    if not strategy:
        raise ValueError("策略版本不存在")
    market = frame if frame is not None else _market_history()
    days = day_frames or _partition_days(market)
    schedule = selections or _selection_schedule(days, {group_id: strategy})
    dates = sorted(days)
    if len(dates) < 120:
        raise ValueError("有效交易日不足120日，不能进行样本外验证")
    split = max(60, int(len(dates) * .6))
    windows = {"inSample": (str(dates[59]), str(dates[split - 1])), "outOfSample": (str(dates[split]), str(dates[-1]))}
    cases: dict[str, Any] = {}
    for mode in MODES:
        for slippage in SLIPPAGE_CASES:
            key = f"{mode}:{slippage}bps"
            cases[key] = {
                name: simulate(market, group_id, strategy, mode, slippage, start=start, end=end, day_frames=days, selections=schedule)
                for name, (start, end) in windows.items()
            }
    baseline = cases["current_snapshot:0bps"]["outOfSample"]
    cost_case = cases["current_snapshot:5bps"]["outOfSample"]
    oos_dates = dates[split:]
    fold_size = max(len(oos_dates) // 3, 1)
    walk_forward = []
    for fold in range(3):
        first = fold * fold_size
        last = len(oos_dates) if fold == 2 else min((fold + 1) * fold_size, len(oos_dates))
        if first >= last:
            continue
        walk_forward.append(simulate(
            market, group_id, strategy, "current_snapshot", 5,
            start=oos_dates[first], end=oos_dates[last - 1], day_frames=days, selections=schedule,
        ))
    parent_comparison: dict[str, Any] | None = None
    parent_version = strategy.get("parent_version")
    if parent_version:
        parent = read_json(root / "strategy_versions" / f"v{int(parent_version)}.json", {})
        if parent:
            parent_schedule = _selection_schedule(days, {group_id: parent})
            parent_result = simulate(
                market, group_id, parent, "current_snapshot", 5,
                start=windows["outOfSample"][0], end=windows["outOfSample"][1], day_frames=days, selections=parent_schedule,
            )
            parent_comparison = {
                "parentVersion": int(parent_version), "candidate": cost_case, "parent": parent_result,
                "returnDifferencePctPoints": round(float(cost_case.get("totalReturnPct", 0)) - float(parent_result.get("totalReturnPct", 0)), 4),
                "sharpeDifference": round(float(cost_case.get("sharpe", 0)) - float(parent_result.get("sharpe", 0)), 4),
                "drawdownDifferencePctPoints": round(float(cost_case.get("maxDrawdownPct", 0)) - float(parent_result.get("maxDrawdownPct", 0)), 4),
            }
    reasons: list[str] = []
    if not baseline.get("eligibleData"):
        reasons.extend(baseline.get("reasons") or ["关键数据不足"])
    elif baseline.get("trades", 0) < 3:
        reasons.append("样本外完成卖出交易少于3笔")
    if cost_case.get("eligibleData") and cost_case.get("totalReturnPct", 0) <= 0:
        reasons.append("计入5bps滑点后样本外收益不为正")
    if cost_case.get("eligibleData") and cost_case.get("sharpe", 0) <= 0:
        reasons.append("计入5bps滑点后样本外Sharpe不为正")
    if baseline.get("maxDrawdownPct", 0) < -25:
        reasons.append("样本外最大回撤超过25%")
    if walk_forward and sum(1 for item in walk_forward if item.get("totalReturnPct", 0) > 0) < 2:
        reasons.append("三个样本外滚动窗口中少于两个取得正收益")
    if parent_comparison:
        if parent_comparison["returnDifferencePctPoints"] < -2:
            reasons.append("候选策略样本外收益比父版本低超过2个百分点")
        if parent_comparison["sharpeDifference"] <= 0 and parent_comparison["drawdownDifferencePctPoints"] <= 0:
            reasons.append("候选策略相对父版本未改善Sharpe或最大回撤")
    result = {
        "schemaVersion": 1, "generatedAt": now_iso(), "groupId": group_id, "version": version,
        "dataRange": {"first": str(dates[0]), "last": str(dates[-1]), "tradingDays": len(dates)},
        "split": {"method": "chronological_60_40", **{key: {"start": value[0], "end": value[1]} for key, value in windows.items()}},
        "assumptions": {"signal": "收盘后生成；最早下一交易日成交", "current_snapshot": "下一交易日开盘价近似", "next_snapshot": "下一交易日收盘价近似", "stopTakeConflict": "同日同时触发时先按止损，采用审慎假设", "gapStop": "止损跳空时按更不利的开盘价", "pit": "announce_date严格早于信号日", "priceSeries": "使用复权OHLC；现金分红与送转数量未单独建模", "excludedBoards": ["STAR", "CHINEXT", "BSE"], "excludedStatus": ["ST", "退市"]},
        "cases": cases, "walkForward": walk_forward, "parentComparison": parent_comparison,
        "eligibilityRules": ["5bps样本外收益和Sharpe均为正", "样本外至少3笔完成交易", "最大回撤不超过25%", "三个滚动窗口至少两个正收益", "候选不显著落后父版本且改善Sharpe或回撤"],
        "eligible": not reasons, "reasons": reasons,
    }
    write_json(root / "strategy_evidence" / f"v{version}" / "backtest.json", result)
    return result


class BacktestService:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        return read_json(STATUS_PATH, {"state": "idle", "updatedAt": None, "completed": 0, "total": 0})

    def start(self) -> dict[str, Any]:
        if self._lock.locked():
            raise ValueError("回测正在运行")
        threading.Thread(target=self._run, daemon=True).start()
        return {"state": "starting"}

    def _run(self) -> None:
        with self._lock:
            tasks: list[tuple[str, int]] = []
            for group_id in GROUP_IDS:
                for path in (LEAGUE_DIR / "groups" / group_id / "strategy_versions").glob("v*.json"):
                    tasks.append((group_id, int(path.stem[1:])))
            write_json(STATUS_PATH, {"state": "running", "startedAt": now_iso(), "updatedAt": now_iso(), "completed": 0, "total": len(tasks), "current": None, "errors": []})
            errors = []
            try:
                frame = _market_history()
                days = _partition_days(frame)
                for index, (group_id, version) in enumerate(tasks, start=1):
                    write_json(STATUS_PATH, {**self.status(), "current": f"{group_id} v{version}", "updatedAt": now_iso()})
                    try:
                        evaluate_version(group_id, version, frame, days)
                    except Exception as exc:
                        errors.append({"groupId": group_id, "version": version, "error": str(exc)})
                    write_json(STATUS_PATH, {**self.status(), "completed": index, "errors": errors, "updatedAt": now_iso()})
                write_json(STATUS_PATH, {**self.status(), "state": "completed" if not errors else "completed_with_errors", "current": None, "finishedAt": now_iso(), "updatedAt": now_iso()})
            except Exception as exc:
                write_json(STATUS_PATH, {**self.status(), "state": "failed", "error": str(exc), "finishedAt": now_iso(), "updatedAt": now_iso()})


backtest_service = BacktestService()
