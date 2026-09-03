from datetime import date

import polars as pl

import app.selection as selection
from app.selection import _dividend_snapshot, _factor_snapshot, _select_groups
from app.storage import GROUP_IDS


def test_selection_universe_excludes_st_delisting_and_star_market():
    trade_date, frame = _factor_snapshot("2026-09-02")
    assert trade_date == "2026-09-02"
    assert frame.height > 0
    assert not any(code.startswith(("688", "689")) for code in frame["code"].to_list())
    assert not any("ST" in name.upper() or "退" in name for name in frame["name"].to_list())


def test_all_group_results_are_explicit(monkeypatch):
    monkeypatch.setattr(selection, "_valuation_snapshot", lambda *_: selection._empty_valuations())
    _, frame = _factor_snapshot("2026-09-02")
    groups = _select_groups(frame, "2026-09-02")
    assert set(groups) == set(GROUP_IDS)
    assert groups["S-A"]["items"]
    assert groups["L-A"]["status"] == "blocked"
    assert "PE TTM" in groups["L-A"]["notes"][-1]
    assert groups["L-B"]["status"] == "ready"
    assert groups["L-B"]["items"]
    assert groups["L-B"]["items"][0]["dividend_yield"] > 0


def test_l_a_runs_when_positive_pe_is_available():
    _, frame = _factor_snapshot("2026-09-02")
    groups = _select_groups(frame.with_columns(
        pl.lit(12.0).alias("pe_ttm"), pl.lit("0").alias("valuation_is_st"),
        pl.lit(date(2026, 9, 2)).alias("valuation_date"), pl.lit("baostock").alias("valuation_source"),
    ), "2026-09-02")

    assert groups["L-A"]["status"] == "ready"
    assert groups["L-A"]["items"]
    assert all(item["pe_ttm"] == 12.0 for item in groups["L-A"]["items"])
    assert all(item["valuation_date"] == "2026-09-02" for item in groups["L-A"]["items"])
    assert "估值来源：baostock 2026-09-02" in groups["L-A"]["notes"]


def test_dividend_snapshot_uses_only_implemented_events_in_lookback(monkeypatch, tmp_path):
    root = tmp_path / "adj_factor"
    root.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"] * 4,
        "ex_date": [date(2025, 2, 28), date(2025, 3, 1), date(2026, 2, 28), date(2026, 3, 2)],
        "dividend_per_share": [9.0, 0.2, 0.3, 8.0],
    }).write_parquet(root / "events.parquet")
    monkeypatch.setattr(selection, "DATA_DIR", tmp_path)

    result = _dividend_snapshot("2026-03-01")

    assert result.to_dicts() == [{"symbol": "600000.SH", "dividend_ttm": 0.5, "dividend_events_ttm": 2}]
