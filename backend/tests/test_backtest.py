from datetime import date

import polars as pl

import app.backtest as backtest
from app.backtest import _entry_band, _join_historical_valuations, _join_trailing_dividends, _summary


def test_entry_band_uses_strategy_volatility_parameters():
    candidate = {"close": 10, "volatility60": .31749}
    strategy = {"parameters": {"entry_price": {"min_daily_vol_pct": .008, "max_daily_vol_pct": .05, "lower_vol_multiplier": -1, "upper_vol_multiplier": 2}}}
    low, high = _entry_band(candidate, strategy)
    assert round(low, 2) == 9.80
    assert round(high, 2) == 10.40


def test_negative_result_is_reported_without_cosmetic_rounding():
    result = _summary([100000, 99000, 95000], [], 100000)
    assert result["totalReturnPct"] == -5
    assert result["maxDrawdownPct"] == -5


def test_backtest_dividend_yield_is_point_in_time(monkeypatch, tmp_path):
    root = tmp_path / "adj_factor"
    root.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        "ex_date": [date(2025, 3, 1), date(2026, 2, 28), date(2026, 3, 2)],
        "dividend_per_share": [0.2, 0.3, 8.0],
    }).write_parquet(root / "events.parquet")
    monkeypatch.setattr(backtest, "DATA_DIR", tmp_path)
    market = pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        "date": [date(2025, 3, 1), date(2026, 2, 28), date(2026, 3, 1)],
        "close": [10.0, 10.0, 10.0],
        "raw_close": [10.0, 10.0, 10.0],
    })

    result = _join_trailing_dividends(market)

    latest = result.filter(pl.col("date") == date(2026, 3, 1)).to_dicts()[0]
    assert latest["dividend_ttm"] == 0.5
    assert latest["dividend_events_ttm"] == 2
    assert latest["dividend_yield"] == 0.05


def test_historical_pe_joins_only_the_exact_trade_date(monkeypatch, tmp_path):
    root = tmp_path / "valuations" / "history"
    root.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "date": [date(2026, 3, 1), date(2026, 3, 3)],
        "pe_ttm": [8.0, 9.0],
        "source": ["baostock", "baostock"],
        "is_st": ["0", "0"],
    }).write_parquet(root / "batch-test.parquet")
    monkeypatch.setattr(backtest, "DATA_DIR", tmp_path)
    market = pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        "date": [date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)],
        "close": [10.0, 10.0, 10.0],
    })

    result = _join_historical_valuations(market).sort("date").to_dicts()

    assert [row["pe_ttm"] for row in result] == [8.0, None, 9.0]
