import json
from datetime import datetime
from zoneinfo import ZoneInfo

import app.execution as execution


SHANGHAI = ZoneInfo("Asia/Shanghai")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def engine_at(tmp_path, *, candidate_symbol="600000.SH", candidate_name="浦发银行"):
    root = tmp_path / "research_league"
    write(root / "league_state.json", {"status": "running"})
    write(root / "rules.json", execution.DEFAULT_RULES)
    group = root / "groups" / "S-A"
    write(group / "config.json", {"group_id": "S-A", "strategy_version": 2, "status": "running"})
    write(group / "strategy_versions" / "v2.json", {
        "version": 2,
        "parameters": {"stop_loss_pct": -0.06, "take_profit_pct": 0.12, "max_holding_days": 10},
    })
    write(group / "account.json", {"initial_cash": 100000, "cash": 100000, "market_value": 0, "nav": 100000, "realized_pnl": 0, "fees_paid": 0})
    write(group / "positions.json", [])
    write(group / "orders.json", [])
    write(group / "trades.json", [])
    write(group / "decisions.json", [])
    write(group / "candidates.json", {
        "status": "ready", "strategy_version": 2, "as_of": "2026-09-02",
        "items": [{"symbol": candidate_symbol, "name": candidate_name, "score": 90, "entry_price_min": 9.8, "entry_price_max": 10.1}],
    })
    return execution.ExecutionEngine(root=root, group_ids=("S-A",))


def quote(price, *, previous=10.0, turnover=1_000_000):
    return {"symbol": "600000.SH", "last_price": price, "prev_close": previous, "turnover": turnover, "quote_source": "test"}


def test_current_snapshot_buys_in_lots_and_revalues(tmp_path):
    engine = engine_at(tmp_path)
    at = datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI)

    result = engine.run_cycle([quote(10.0)], at=at, force=True)
    position = json.loads((engine.root / "groups" / "S-A" / "positions.json").read_text(encoding="utf-8"))[0]
    account = json.loads((engine.root / "groups" / "S-A" / "account.json").read_text(encoding="utf-8"))

    assert result["state"] == "completed"
    assert position["quantity"] % 100 == 0
    assert position["strategy_version"] == 2
    assert position["current_price"] == 10.0
    assert position["slippage_bps"] == 0
    assert account["fees_paid"] > 0
    assert account["nav"] < 100000


def test_t_plus_one_blocks_same_day_then_records_sell_reason(tmp_path):
    engine = engine_at(tmp_path)
    engine.run_cycle([quote(10.0)], at=datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI), force=True)
    engine.run_cycle([quote(9.3)], at=datetime(2026, 9, 2, 14, 0, tzinfo=SHANGHAI), force=True)
    assert len(json.loads((engine.root / "groups" / "S-A" / "positions.json").read_text(encoding="utf-8"))) == 1

    engine.run_cycle([quote(9.3)], at=datetime(2026, 9, 3, 10, 0, tzinfo=SHANGHAI), force=True)
    trades = json.loads((engine.root / "groups" / "S-A" / "trades.json").read_text(encoding="utf-8"))

    assert len(trades) == 1
    assert trades[0]["sell_reason"] == "固定止损触发"
    assert trades[0]["buy_time"] == "2026-09-02T10:00:00+08:00"
    assert trades[0]["sell_time"] == "2026-09-03T10:00:00+08:00"


def test_limit_down_blocks_triggered_exit(tmp_path):
    engine = engine_at(tmp_path)
    engine.run_cycle([quote(10.0)], at=datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI), force=True)
    engine.run_cycle([quote(9.0)], at=datetime(2026, 9, 3, 10, 0, tzinfo=SHANGHAI), force=True)

    positions = json.loads((engine.root / "groups" / "S-A" / "positions.json").read_text(encoding="utf-8"))
    orders = json.loads((engine.root / "groups" / "S-A" / "orders.json").read_text(encoding="utf-8"))
    assert len(positions) == 1
    assert orders[-1]["status"] == "blocked"
    assert orders[-1]["blocked_reason"] == "跌停不可卖"


def test_excluded_board_never_opens_new_position(tmp_path):
    engine = engine_at(tmp_path, candidate_symbol="300001.SZ", candidate_name="特锐德")
    candidate_quote = {**quote(10.0), "symbol": "300001.SZ"}

    engine.run_cycle([candidate_quote], at=datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI), force=True)

    positions = json.loads((engine.root / "groups" / "S-A" / "positions.json").read_text(encoding="utf-8"))
    decisions = json.loads((engine.root / "groups" / "S-A" / "decisions.json").read_text(encoding="utf-8"))
    assert positions == []
    assert decisions[-1]["reason"] == "创业板已排除"


def test_unknown_calendar_year_fails_closed(tmp_path):
    engine = engine_at(tmp_path)
    result = engine.run_cycle([quote(10.0)], at=datetime(2027, 9, 2, 10, 0, tzinfo=SHANGHAI), force=True)
    assert result["state"] == "blocked"
    assert result["reason"] == "交易日历未覆盖2027年"


def test_strategy_cycle_is_throttled_but_quote_refresh_revalues(tmp_path):
    engine = engine_at(tmp_path)
    first = datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI)
    engine.run_cycle([quote(10.0)], at=first, force=True)
    result = engine.run_cycle([quote(10.05)], at=datetime(2026, 9, 2, 10, 5, tzinfo=SHANGHAI))

    position = json.loads((engine.root / "groups" / "S-A" / "positions.json").read_text(encoding="utf-8"))[0]
    assert result["state"] == "throttled"
    assert position["current_price"] == 10.05


def test_next_snapshot_mode_waits_for_next_strategy_evaluation(tmp_path):
    engine = engine_at(tmp_path)
    service = execution.ExecutionService(engine=engine)
    service.set_mode("next_snapshot")
    engine.run_cycle([quote(10.0)], at=datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI), force=True)
    assert json.loads((engine.root / "groups" / "S-A" / "positions.json").read_text(encoding="utf-8")) == []

    engine.run_cycle([quote(10.05)], at=datetime(2026, 9, 2, 10, 10, tzinfo=SHANGHAI))
    position = json.loads((engine.root / "groups" / "S-A" / "positions.json").read_text(encoding="utf-8"))[0]
    assert position["buy_price"] == 10.05
    assert position["execution_mode"] == "next_snapshot"


def test_slippage_direction_and_stale_watermark_are_explicit():
    rules = {**execution.DEFAULT_RULES, "slippage_bps": 10}
    assert execution.ExecutionEngine._fill_price(10.0, "buy", rules) == 10.01
    assert execution.ExecutionEngine._fill_price(10.0, "sell", rules) == 9.99

    now = datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI)
    stale = datetime(2026, 9, 2, 9, 50, tzinfo=SHANGHAI)
    try:
        execution.validated_market_time(int(stale.timestamp() * 1000), now, 120)
    except ValueError as exc:
        assert "已过期" in str(exc)
    else:
        raise AssertionError("陈旧行情水位必须被拒绝")


def test_initialize_creates_only_missing_empty_simulation_state(tmp_path):
    root = tmp_path / "research_league"
    write(root / "groups" / "S-A" / "config.json", {"group_id": "S-A"})
    service = execution.ExecutionService(engine=execution.ExecutionEngine(root=root, group_ids=("S-A",)))

    service.initialize()
    account_path = root / "groups" / "S-A" / "account.json"
    account = json.loads(account_path.read_text(encoding="utf-8"))
    assert json.loads((root / "league_state.json").read_text(encoding="utf-8"))["status"] == "running"
    assert account["initial_cash"] == 100000
    assert account["cash"] == 100000

    account["cash"] = 12345
    write(account_path, account)
    service.initialize()
    assert json.loads(account_path.read_text(encoding="utf-8"))["cash"] == 12345
