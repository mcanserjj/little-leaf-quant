from __future__ import annotations

import math
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

from .config import HITHINK_BASE_URL, LEAGUE_DIR
from .secrets import load_hithink_key
from .storage import GROUP_IDS, now_iso, read_json, write_json

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUOTE_PATH = LEAGUE_DIR / "shared" / "market" / "latest-quotes.json"
EXECUTION_SERVICE_PATH = LEAGUE_DIR / "execution_service.json"

DEFAULT_RULES: dict[str, Any] = {
    "market": "CN_A_SHARE",
    "initial_cash": 100000.0,
    "max_positions": 5,
    "buy_lot_size": 100,
    "t_plus_one": True,
    "market_quote_refresh_seconds": 5,
    "strategy_evaluation_seconds": 600,
    "execution_mode": "current_snapshot",
    "execution_modes": ["current_snapshot", "next_snapshot"],
    "slippage_bps": 0,
    "slippage_is_user_assumption": True,
    "max_quote_age_seconds": 120,
    "fees": {
        "broker_commission_rate": 0.00025,
        "broker_commission_min": 5.0,
        "transfer_fee_rate": 0.00001,
        "stamp_duty_sell_rate": 0.0005,
    },
    "universe": {
        "exclude_st": True,
        "exclude_delisting": True,
        "exclude_boards": ["STAR", "CHINEXT", "BSE"],
    },
}

# 只声明已经由交易所正式公布的年份。未覆盖年份严格停止自动撮合。
MARKET_HOLIDAYS: dict[int, set[date]] = {
    2026: {
        date(2026, 1, 1), date(2026, 1, 2),
        *{date(2026, 2, day) for day in range(16, 24)},
        date(2026, 4, 6),
        *{date(2026, 5, day) for day in range(1, 6)},
        date(2026, 6, 19),
        date(2026, 9, 25),
        *{date(2026, 10, day) for day in range(1, 8)},
    },
}


def _local(at: datetime | None = None) -> datetime:
    value = at or datetime.now(SHANGHAI)
    return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)


def market_gate(at: datetime | None = None) -> tuple[bool, str]:
    local = _local(at)
    if local.year not in MARKET_HOLIDAYS:
        return False, f"交易日历未覆盖{local.year}年"
    if local.weekday() >= 5 or local.date() in MARKET_HOLIDAYS[local.year]:
        return False, "非交易日"
    clock = local.time().replace(tzinfo=None)
    if not (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock <= time(15)):
        return False, "非连续竞价时段"
    return True, "交易时段"


def validated_market_time(timestamp_ms: Any, now: datetime, max_age_seconds: int) -> datetime:
    if timestamp_ms is None:
        raise ValueError("HiThink全市场行情未返回可核验时间戳")
    market_time = datetime.fromtimestamp(int(timestamp_ms) / 1000, SHANGHAI)
    age = (_local(now) - market_time).total_seconds()
    if age > max_age_seconds or age < -60:
        raise ValueError(f"HiThink行情水位时间异常或已过期：{market_time.isoformat(timespec='seconds')}")
    return market_time


def _board_reason(symbol: str, name: str, rules: dict[str, Any]) -> str | None:
    universe = rules.get("universe", {})
    upper_name = name.upper()
    if universe.get("exclude_st", True) and "ST" in upper_name:
        return "ST股票已排除"
    if universe.get("exclude_delisting", True) and "退" in name:
        return "退市风险股票已排除"
    excluded = set(universe.get("exclude_boards", []))
    code = symbol.split(".", 1)[0]
    if "STAR" in excluded and code.startswith(("688", "689")):
        return "科创板已排除"
    if "CHINEXT" in excluded and code.startswith(("300", "301")):
        return "创业板已排除"
    if "BSE" in excluded and (symbol.endswith(".BJ") or code.startswith(("4", "8", "92"))):
        return "北交所已排除"
    return None


def _limit_pct(symbol: str, name: str) -> float:
    code = symbol.split(".", 1)[0]
    if "ST" in name.upper():
        return 0.05
    if symbol.endswith(".BJ") or code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


class ExecutionEngine:
    def __init__(self, root: Path = LEAGUE_DIR, group_ids: Iterable[str] = GROUP_IDS):
        self.root = root
        self.group_ids = tuple(group_ids)
        self._lock = threading.Lock()

    def rules(self) -> dict[str, Any]:
        stored = read_json(self.root / "rules.json", {})
        return {**DEFAULT_RULES, **stored, "fees": {**DEFAULT_RULES["fees"], **stored.get("fees", {})}, "universe": {**DEFAULT_RULES["universe"], **stored.get("universe", {})}}

    @staticmethod
    def _fees(amount: float, side: str, rules: dict[str, Any]) -> dict[str, float]:
        cfg = rules["fees"]
        commission = max(float(cfg["broker_commission_min"]), amount * float(cfg["broker_commission_rate"]))
        transfer = amount * float(cfg["transfer_fee_rate"])
        stamp = amount * float(cfg["stamp_duty_sell_rate"]) if side == "sell" else 0.0
        return {"commission": round(commission, 2), "transfer_fee": round(transfer, 2), "stamp_duty": round(stamp, 2), "total": round(commission + transfer + stamp, 2)}

    @staticmethod
    def _fill_price(snapshot_price: float, side: str, rules: dict[str, Any]) -> float:
        slip = float(rules.get("slippage_bps", 0)) / 10_000
        adjusted = snapshot_price * (1 + slip if side == "buy" else 1 - slip)
        return round(adjusted + 1e-10, 2)

    @staticmethod
    def _tradable(quote: dict[str, Any] | None, side: str, symbol: str, name: str) -> tuple[bool, str | None]:
        try:
            price = float((quote or {}).get("last_price"))
            previous = float((quote or {}).get("prev_close"))
            turnover = float((quote or {}).get("turnover"))
        except (TypeError, ValueError):
            return False, "行情字段不完整"
        if not all(math.isfinite(value) and value > 0 for value in (price, previous, turnover)):
            return False, "停牌或行情无效"
        limit = _limit_pct(symbol, name)
        if side == "buy" and price >= round(previous * (1 + limit) + 1e-8, 2) - 0.005:
            return False, "涨停不可买"
        if side == "sell" and price <= round(previous * (1 - limit) + 1e-8, 2) + 0.005:
            return False, "跌停不可卖"
        return True, None

    def _strategy(self, group_dir: Path, version: int) -> dict[str, Any]:
        return read_json(group_dir / "strategy_versions" / f"v{version}.json", {})

    def refresh_quotes(self, quotes: list[dict[str, Any]], at: datetime | None = None) -> None:
        local = _local(at)
        quote_map = {str(row.get("symbol")): row for row in quotes if row.get("symbol")}
        write_json(QUOTE_PATH if self.root == LEAGUE_DIR else self.root / "shared" / "market" / "latest-quotes.json", {"fetchedAt": local.isoformat(timespec="seconds"), "source": "hithink" if any(row.get("quote_source") == "hithink" for row in quotes) else "test", "items": quotes})
        for group_id in self.group_ids:
            group_dir = self.root / "groups" / group_id
            account = read_json(group_dir / "account.json", {})
            positions = read_json(group_dir / "positions.json", [])
            if not account or not isinstance(positions, list):
                continue
            market_value = 0.0
            for position in positions:
                quote = quote_map.get(str(position.get("symbol")))
                price = float((quote or {}).get("last_price") or position.get("current_price") or position.get("buy_price") or 0)
                position["current_price"] = price
                position["quote_time"] = local.isoformat(timespec="seconds") if quote else position.get("quote_time")
                position["quote_source"] = (quote or {}).get("quote_source", position.get("quote_source"))
                position["highest_price"] = max(float(position.get("highest_price") or position.get("buy_price") or price), price)
                basis = float(position.get("buy_price") or 0) * int(position.get("quantity") or 0) + float(position.get("buy_fees") or 0)
                value = price * int(position.get("quantity") or 0)
                position["market_value"] = round(value, 2)
                position["unrealized_pnl"] = round(value - basis, 2)
                position["return_pct"] = round((value - basis) / basis * 100, 6) if basis else None
                market_value += value
            account.update({"market_value": round(market_value, 2), "nav": round(float(account.get("cash") or 0) + market_value, 2), "updated_at": local.isoformat(timespec="seconds")})
            write_json(group_dir / "positions.json", positions)
            write_json(group_dir / "account.json", account)

    def run_cycle(self, quotes: list[dict[str, Any]], *, at: datetime | None = None, force: bool = False) -> dict[str, Any]:
        local = _local(at)
        self.refresh_quotes(quotes, local)
        allowed, reason = market_gate(local)
        if not allowed:
            return {"state": "blocked", "reason": reason, "at": local.isoformat(timespec="seconds")}
        if read_json(self.root / "league_state.json", {}).get("status") != "running":
            return {"state": "paused", "reason": "联赛未运行", "at": local.isoformat(timespec="seconds")}
        rules = self.rules()
        state_path = self.root / "execution_state.json"
        state = read_json(state_path, {})
        previous = state.get("last_evaluation_at") or state.get("last_cycle_at")
        interval = int(rules.get("strategy_evaluation_seconds") or rules.get("quote_refresh_seconds") or 600)
        if not force and previous:
            elapsed = (local - datetime.fromisoformat(previous).astimezone(SHANGHAI)).total_seconds()
            if elapsed < interval:
                return {"state": "throttled", "reason": f"距离下次策略评估还有{max(0, interval - int(elapsed))}秒", "at": local.isoformat(timespec="seconds")}
        quote_map = {str(row.get("symbol")): row for row in quotes if row.get("symbol")}
        run_id = f"execution-{local:%Y%m%d-%H%M%S}"
        with self._lock:
            for group_id in self.group_ids:
                self._run_group(group_id, quote_map, local, rules, run_id)
            self.refresh_quotes(quotes, local)
            state = {"last_cycle_at": local.isoformat(timespec="seconds"), "last_evaluation_at": local.isoformat(timespec="seconds"), "last_quote_at": local.isoformat(timespec="seconds"), "quote_count": len(quote_map), "last_run_id": run_id, "state": "completed"}
            write_json(state_path, state)
            write_json(self.root / "shared" / "market" / "execution_snapshots" / f"{run_id}.json", {"runId": run_id, "quoteTime": local.isoformat(timespec="seconds"), "items": quotes})
        return {"state": "completed", "runId": run_id, "quoteCount": len(quote_map), "at": local.isoformat(timespec="seconds")}

    def _run_group(self, group_id: str, quotes: dict[str, dict[str, Any]], at: datetime, rules: dict[str, Any], run_id: str) -> None:
        group_dir = self.root / "groups" / group_id
        config = read_json(group_dir / "config.json", {})
        account = read_json(group_dir / "account.json", {})
        if not config or not account:
            return
        positions = read_json(group_dir / "positions.json", [])
        orders = read_json(group_dir / "orders.json", [])
        trades = read_json(group_dir / "trades.json", [])
        decisions = read_json(group_dir / "decisions.json", [])
        candidates = read_json(group_dir / "candidates.json", {})
        now_text = at.isoformat(timespec="seconds")
        mode = rules.get("execution_mode", "current_snapshot")

        for position in positions:
            position["available_quantity"] = int(position.get("quantity") or 0) if str(position.get("buy_trade_date", "")) < at.date().isoformat() else 0

        for order in orders:
            if order.get("status") != "pending" or order.get("execution_mode") != "next_snapshot" or str(order.get("signal_time")) >= now_text:
                continue
            quote = quotes.get(str(order.get("symbol")))
            if order.get("side") == "buy" and not self._in_entry_range(order, quote):
                order.update({"status": "cancelled", "blocked_reason": "下一快照已离开策略买入区间"})
                continue
            position = next((row for row in positions if row.get("symbol") == order.get("symbol")), None)
            name = str(order.get("name") or (position or {}).get("name") or "")
            ok, blocked = self._tradable(quote, str(order.get("side")), str(order.get("symbol")), name)
            if not ok:
                order["blocked_reason"] = blocked
                continue
            if order.get("side") == "buy" and position is None:
                self._fill_buy(order, quote or {}, positions, account, at, rules, config, run_id)
            elif order.get("side") == "sell" and position and position.get("available_quantity", 0) > 0:
                self._fill_sell(order, quote or {}, position, positions, trades, account, at, rules, run_id)

        pending_sells = {row.get("symbol") for row in orders if row.get("status") in {"pending", "blocked"} and row.get("side") == "sell"}
        for order in orders:
            if order.get("status") != "blocked" or order.get("side") != "sell" or order.get("execution_mode") != "current_snapshot":
                continue
            position = next((row for row in positions if row.get("symbol") == order.get("symbol")), None)
            quote = quotes.get(str(order.get("symbol")))
            if not position or position.get("available_quantity", 0) <= 0:
                continue
            ok, blocked = self._tradable(quote, "sell", str(order.get("symbol")), str(position.get("name", "")))
            if ok:
                order["status"] = "pending"
                order.pop("blocked_reason", None)
                self._fill_sell(order, quote or {}, position, positions, trades, account, at, rules, run_id)
                pending_sells.discard(order.get("symbol"))
            else:
                order["blocked_reason"] = blocked
        for position in list(positions):
            symbol = str(position.get("symbol"))
            quote = quotes.get(symbol)
            if not quote or quote.get("last_price") is None or symbol in pending_sells:
                continue
            price = float(quote["last_price"])
            buy_price = float(position.get("buy_price") or 0)
            strategy = self._strategy(group_dir, int(position.get("strategy_version") or config.get("strategy_version") or 1))
            params = strategy.get("parameters", {})
            return_pct = price / buy_price - 1 if buy_price else 0
            held_days = (at.date() - datetime.fromisoformat(str(position["buy_time"])).date()).days
            sell_reason = None
            if return_pct <= float(params.get("stop_loss_pct", -0.06)):
                sell_reason = "固定止损触发"
            elif return_pct >= float(params.get("take_profit_pct", 0.12)):
                sell_reason = "目标收益触发"
            elif held_days >= int(params.get("max_holding_days", 10)):
                sell_reason = "最大持有期触发"
            if not sell_reason:
                continue
            if position.get("available_quantity", 0) <= 0:
                decisions.append({"time": now_text, "symbol": symbol, "action": "hold", "reason": "T+1当日不可卖"})
                continue
            order = self._order(group_id, "sell", symbol, int(position["quantity"]), now_text, mode, sell_reason, run_id, position.get("name", ""))
            orders.append(order)
            if mode == "current_snapshot":
                ok, blocked = self._tradable(quote, "sell", symbol, str(position.get("name", "")))
                if ok:
                    self._fill_sell(order, quote, position, positions, trades, account, at, rules, run_id)
                else:
                    order.update({"status": "blocked", "blocked_reason": blocked})

        active_version = int(config.get("strategy_version") or 1)
        candidate_date_valid = str(candidates.get("as_of") or "") <= at.date().isoformat()
        if candidates.get("status") == "ready" and candidate_date_valid and int(candidates.get("strategy_version") or 0) == active_version:
            held = {row.get("symbol") for row in positions}
            pending_buys = {row.get("symbol") for row in orders if row.get("status") == "pending" and row.get("side") == "buy"}
            slots = max(int(rules.get("max_positions", 5)) - len(positions) - len(pending_buys), 0)
            for item in candidates.get("items", []):
                symbol = str(item.get("symbol") or "")
                if not symbol or symbol in held or symbol in pending_buys or slots <= 0:
                    continue
                excluded = _board_reason(symbol, str(item.get("name", "")), rules)
                if excluded:
                    decisions.append({"time": now_text, "symbol": symbol, "action": "blocked", "reason": excluded})
                    continue
                quote = quotes.get(symbol)
                if not self._in_entry_range(item, quote):
                    decisions.append({"time": now_text, "symbol": symbol, "action": "wait", "reason": "现价未进入策略买入区间", "current_price": (quote or {}).get("last_price"), "entry_price_min": item.get("entry_price_min"), "entry_price_max": item.get("entry_price_max")})
                    continue
                ok, blocked = self._tradable(quote, "buy", symbol, str(item.get("name", "")))
                if not ok:
                    decisions.append({"time": now_text, "symbol": symbol, "action": "blocked", "reason": blocked})
                    continue
                price = float((quote or {})["last_price"])
                target = float(account.get("nav") or rules.get("initial_cash", 100000)) / int(rules.get("max_positions", 5))
                quantity = math.floor(min(target, float(account.get("cash") or 0)) / price / int(rules.get("buy_lot_size", 100))) * int(rules.get("buy_lot_size", 100))
                if quantity <= 0:
                    decisions.append({"time": now_text, "symbol": symbol, "action": "blocked", "reason": "现金不足100股"})
                    continue
                order = self._order(group_id, "buy", symbol, quantity, now_text, mode, "现价进入策略买入区间", run_id, str(item.get("name", "")))
                order.update({"entry_price_min": item.get("entry_price_min"), "entry_price_max": item.get("entry_price_max"), "entry_reference_price": item.get("entry_reference_price")})
                orders.append(order)
                slots -= 1
                if mode == "current_snapshot":
                    self._fill_buy(order, quote or {}, positions, account, at, rules, config, run_id)
        elif candidates.get("status") == "ready":
            reason = "候选数据截止日在当前交易日之后" if not candidate_date_valid else "候选策略版本不是当前生效版本，请重新运行选股"
            decisions.append({"time": now_text, "action": "blocked", "reason": reason})

        write_json(group_dir / "config.json", {**config, "status": "running"})
        write_json(group_dir / "account.json", account)
        write_json(group_dir / "positions.json", positions)
        write_json(group_dir / "orders.json", orders[-2000:])
        write_json(group_dir / "trades.json", trades[-2000:])
        write_json(group_dir / "decisions.json", decisions[-5000:])

    @staticmethod
    def _in_entry_range(item: dict[str, Any], quote: dict[str, Any] | None) -> bool:
        try:
            price = float((quote or {}).get("last_price"))
            lower = float(item.get("entry_price_min"))
            upper = float(item.get("entry_price_max"))
            return math.isfinite(price) and lower <= price <= upper
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _order(group_id: str, side: str, symbol: str, quantity: int, signal_time: str, mode: str, reason: str, run_id: str, name: str) -> dict[str, Any]:
        return {"order_id": f"{group_id}-{side}-{signal_time}-{symbol}", "side": side, "symbol": symbol, "name": name, "quantity": quantity, "signal_time": signal_time, "execution_mode": mode, "reason": reason, "status": "pending", "run_id": run_id}

    def _fill_buy(self, order: dict[str, Any], quote: dict[str, Any], positions: list[dict[str, Any]], account: dict[str, Any], at: datetime, rules: dict[str, Any], config: dict[str, Any], run_id: str) -> None:
        snapshot_price = float(quote["last_price"])
        price = self._fill_price(snapshot_price, "buy", rules)
        lot = int(rules.get("buy_lot_size", 100))
        quantity = int(order["quantity"])
        fees = self._fees(price * quantity, "buy", rules)
        while quantity >= lot and price * quantity + fees["total"] > float(account.get("cash") or 0):
            quantity -= lot
            fees = self._fees(price * quantity, "buy", rules)
        if quantity <= 0:
            order.update({"status": "blocked", "blocked_reason": "现金不足"})
            return
        account["cash"] = round(float(account.get("cash") or 0) - price * quantity - fees["total"], 2)
        account["fees_paid"] = round(float(account.get("fees_paid") or 0) + fees["total"], 2)
        order.update({"status": "filled", "fill_time": at.isoformat(timespec="seconds"), "snapshot_price": snapshot_price, "fill_price": price, "slippage_bps": float(rules.get("slippage_bps", 0)), "quantity": quantity, "fees": fees, "fill_run_id": run_id, "quote_source": quote.get("quote_source"), "quote_market_time": quote.get("quote_market_time")})
        positions.append({"symbol": order["symbol"], "name": order.get("name", ""), "quantity": quantity, "available_quantity": 0, "buy_time": at.isoformat(timespec="seconds"), "buy_trade_date": at.date().isoformat(), "buy_price": price, "average_cost": price, "buy_fees": fees["total"], "highest_price": snapshot_price, "current_price": snapshot_price, "quote_time": at.isoformat(timespec="seconds"), "quote_market_time": quote.get("quote_market_time"), "strategy_version": int(config.get("strategy_version") or 1), "season_id": at.strftime("%Y-%m"), "execution_mode": order["execution_mode"], "buy_run_id": run_id, "quote_source": quote.get("quote_source"), "slippage_bps": float(rules.get("slippage_bps", 0))})

    def _fill_sell(self, order: dict[str, Any], quote: dict[str, Any], position: dict[str, Any], positions: list[dict[str, Any]], trades: list[dict[str, Any]], account: dict[str, Any], at: datetime, rules: dict[str, Any], run_id: str) -> None:
        snapshot_price = float(quote["last_price"])
        price = self._fill_price(snapshot_price, "sell", rules)
        quantity = int(position["quantity"])
        fees = self._fees(price * quantity, "sell", rules)
        proceeds = price * quantity - fees["total"]
        basis = float(position["buy_price"]) * quantity + float(position.get("buy_fees") or 0)
        pnl = proceeds - basis
        account["cash"] = round(float(account.get("cash") or 0) + proceeds, 2)
        account["realized_pnl"] = round(float(account.get("realized_pnl") or 0) + pnl, 2)
        account["fees_paid"] = round(float(account.get("fees_paid") or 0) + fees["total"], 2)
        order.update({"status": "filled", "fill_time": at.isoformat(timespec="seconds"), "snapshot_price": snapshot_price, "fill_price": price, "slippage_bps": float(rules.get("slippage_bps", 0)), "fees": fees, "fill_run_id": run_id, "quote_source": quote.get("quote_source"), "quote_market_time": quote.get("quote_market_time")})
        trades.append({"trade_id": order["order_id"], "symbol": position["symbol"], "name": position.get("name", ""), "quantity": quantity, "buy_time": position["buy_time"], "buy_price": position["buy_price"], "buy_fees": position.get("buy_fees", 0), "sell_time": at.isoformat(timespec="seconds"), "sell_price": price, "sell_snapshot_price": snapshot_price, "sell_fees": fees["total"], "sell_reason": order["reason"], "slippage_bps": float(rules.get("slippage_bps", 0)), "strategy_version": position.get("strategy_version", 1), "season_id": position.get("season_id", at.strftime("%Y-%m")), "execution_mode": order["execution_mode"], "buy_run_id": position.get("buy_run_id"), "sell_run_id": run_id, "buy_quote_source": position.get("quote_source"), "sell_quote_source": quote.get("quote_source"), "quote_market_time": quote.get("quote_market_time"), "realized_pnl": round(pnl, 2), "return_pct": round(pnl / basis * 100, 6) if basis else None})
        positions.remove(position)


class HiThinkQuoteProvider:
    def fetch(self, symbols: list[str], max_age_seconds: int = 120) -> list[dict[str, Any]]:
        token = load_hithink_key()
        if not token:
            raise ValueError("请先配置HiThink Financial API Key")
        rows: list[dict[str, Any]] = []
        fetched_at = now_iso()
        with httpx.Client(headers={"X-api-key": token}, timeout=30) as client:
            for offset in range(0, len(symbols), 100):
                response = client.get(f"{HITHINK_BASE_URL}/api/a-share/prices/snapshot", params={"thscodes": ",".join(symbols[offset:offset + 100])})
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    raise QuoteRateLimitError(int(retry_after) if retry_after and retry_after.isdigit() else None)
                response.raise_for_status()
                envelope = response.json()
                if envelope.get("code") != 0:
                    raise RuntimeError(f"HiThink code={envelope.get('code')}: {envelope.get('message', '未知错误')}")
                data = envelope.get("data") or {}
                market_time = validated_market_time(data.get("timestamp"), datetime.now(SHANGHAI), max_age_seconds)
                for item in data.get("item") or []:
                    rows.append({"symbol": item.get("thscode"), "last_price": item.get("last_price"), "prev_close": item.get("prev_price"), "open": item.get("open_price"), "high": item.get("high_price"), "low": item.get("low_price"), "volume": item.get("volume"), "turnover": item.get("turnover"), "change_pct": item.get("price_change_ratio_pct"), "quote_source": "hithink", "fetched_at": fetched_at, "quote_market_time": market_time.isoformat(timespec="seconds")})
        return rows


class QuoteRateLimitError(RuntimeError):
    def __init__(self, retry_after: int | None = None):
        super().__init__("HiThink行情接口触发限流")
        self.retry_after = retry_after


class ExecutionService:
    def __init__(self, engine: ExecutionEngine | None = None, provider: HiThinkQuoteProvider | None = None):
        self.engine = engine or ExecutionEngine()
        self.provider = provider or HiThinkQuoteProvider()
        self._lock = threading.Lock()

    def relevant_symbols(self) -> list[str]:
        return sorted(set(self.position_symbols()) | set(self.candidate_symbols()))

    def position_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for group_id in self.engine.group_ids:
            group = self.engine.root / "groups" / group_id
            symbols.update(str(row.get("symbol")) for row in read_json(group / "positions.json", []) if row.get("symbol"))
        return sorted(symbols)

    def candidate_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for group_id in self.engine.group_ids:
            group = self.engine.root / "groups" / group_id
            candidates = read_json(group / "candidates.json", {})
            symbols.update(str(row.get("symbol")) for row in candidates.get("items", []) if row.get("symbol"))
        return sorted(symbols)

    def _evaluation_due(self, local: datetime, rules: dict[str, Any]) -> bool:
        state = read_json(self.engine.root / "execution_state.json", {})
        previous = state.get("last_evaluation_at") or state.get("last_cycle_at")
        if not previous:
            return True
        try:
            elapsed = (local - datetime.fromisoformat(previous).astimezone(SHANGHAI)).total_seconds()
        except (TypeError, ValueError):
            return True
        return elapsed >= int(rules.get("strategy_evaluation_seconds") or rules.get("quote_refresh_seconds") or 600)

    def tick(self, at: datetime | None = None) -> dict[str, Any]:
        local = _local(at)
        allowed, reason = market_gate(local)
        if not allowed:
            return self._save_state({"state": "waiting", "reason": reason, "checkedAt": local.isoformat(timespec="seconds")})
        if read_json(self.engine.root / "league_state.json", {}).get("status") != "running":
            return self._save_state({"state": "paused", "reason": "联赛未运行", "checkedAt": local.isoformat(timespec="seconds")})
        if not load_hithink_key():
            return self._save_state({"state": "blocked", "reason": "HiThink API Key未配置", "checkedAt": local.isoformat(timespec="seconds")})
        service_state = read_json(self._service_path(), {})
        cooldown_until = service_state.get("cooldownUntil")
        if cooldown_until:
            try:
                remaining = int((datetime.fromisoformat(cooldown_until).astimezone(SHANGHAI) - local).total_seconds())
            except (TypeError, ValueError):
                remaining = 0
            if remaining > 0:
                return self._save_state({"state": "rate_limited", "reason": f"HiThink限流冷却中，约{remaining}秒后重试", "checkedAt": local.isoformat(timespec="seconds")})
        rules = self.engine.rules()
        evaluation_due = self._evaluation_due(local, rules)
        symbols = self.relevant_symbols() if evaluation_due else self.position_symbols()
        if not symbols:
            reason = "没有持仓或候选股票" if evaluation_due else "没有持仓，候选行情将在下次策略评估时刷新"
            return self._save_state({"state": "idle", "reason": reason, "checkedAt": local.isoformat(timespec="seconds")})
        if not self._lock.acquire(blocking=False):
            return self.status()
        try:
            quotes = self.provider.fetch(symbols, int(rules.get("max_quote_age_seconds", 120)))
            if evaluation_due:
                result = self.engine.run_cycle(quotes, at=local)
                reason = result.get("reason") or result.get("state")
                scope = "持仓和候选"
            else:
                self.engine.refresh_quotes(quotes, local)
                reason = "持仓行情已刷新；候选行情每10分钟随策略评估刷新"
                scope = "仅持仓"
            return self._save_state({"state": "running", "reason": reason, "checkedAt": local.isoformat(timespec="seconds"), "lastQuoteAt": local.isoformat(timespec="seconds"), "quoteCount": len(quotes), "quoteScope": scope, "lastEvaluationAt": read_json(self.engine.root / "execution_state.json", {}).get("last_evaluation_at"), "rateLimitFailures": 0, "cooldownUntil": None})
        except QuoteRateLimitError as exc:
            failures = int(read_json(self._service_path(), {}).get("rateLimitFailures") or 0) + 1
            delay = exc.retry_after or min(15 * (2 ** (failures - 1)), 120)
            until = local + timedelta(seconds=delay)
            return self._save_state({"state": "rate_limited", "reason": f"HiThink请求过于频繁，已暂停{delay}秒后自动重试", "checkedAt": local.isoformat(timespec="seconds"), "rateLimitFailures": failures, "cooldownUntil": until.isoformat(timespec="seconds")})
        except Exception as exc:
            return self._save_state({"state": "error", "reason": str(exc), "checkedAt": local.isoformat(timespec="seconds")})
        finally:
            self._lock.release()

    def _service_path(self) -> Path:
        return EXECUTION_SERVICE_PATH if self.engine.root == LEAGUE_DIR else self.engine.root / "execution_service.json"

    def initialize(self) -> None:
        league_state = self.engine.root / "league_state.json"
        if not league_state.exists():
            write_json(league_state, {"status": "running", "started_at": now_iso(), "started_manually": False, "note": "首次启动创建的空模拟联赛"})
        initial_cash = float(self.engine.rules().get("initial_cash", 100000))
        for group_id in self.engine.group_ids:
            group_dir = self.engine.root / "groups" / group_id
            if not (group_dir / "config.json").exists() or (group_dir / "account.json").exists():
                continue
            write_json(group_dir / "account.json", {
                "initial_cash": initial_cash,
                "cash": initial_cash,
                "market_value": 0.0,
                "nav": initial_cash,
                "realized_pnl": 0.0,
                "fees_paid": 0.0,
                "updated_at": now_iso(),
            })

    def _save_state(self, value: dict[str, Any]) -> dict[str, Any]:
        current = read_json(self._service_path(), {})
        write_json(self._service_path(), {**current, **value})
        return self.status()

    def status(self) -> dict[str, Any]:
        rules = self.engine.rules()
        state = read_json(self._service_path(), {})
        return {**state, "enabled": read_json(self.engine.root / "league_state.json", {}).get("status") == "running", "providerConfigured": bool(load_hithink_key()), "executionMode": rules.get("execution_mode", "current_snapshot"), "quoteRefreshSeconds": int(rules.get("market_quote_refresh_seconds", 5)), "strategyEvaluationSeconds": int(rules.get("strategy_evaluation_seconds") or rules.get("quote_refresh_seconds") or 600), "calendarYears": sorted(MARKET_HOLIDAYS)}

    def set_mode(self, mode: str) -> dict[str, Any]:
        rules_path = self.engine.root / "rules.json"
        rules = read_json(rules_path, DEFAULT_RULES)
        if mode not in rules.get("execution_modes", DEFAULT_RULES["execution_modes"]):
            raise ValueError("未知成交方式")
        rules["execution_mode"] = mode
        rules.setdefault("market_quote_refresh_seconds", 5)
        rules.setdefault("strategy_evaluation_seconds", int(rules.get("quote_refresh_seconds", 600)))
        write_json(rules_path, rules)
        return self.status()


execution_service = ExecutionService()
