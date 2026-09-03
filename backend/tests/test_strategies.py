import json

import app.strategies as strategies


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_approval_activates_new_entries_without_rewriting_positions(monkeypatch, tmp_path):
    monkeypatch.setattr(strategies, "LEAGUE_DIR", tmp_path)
    root = tmp_path / "groups" / "S-A"
    write(root / "config.json", {"group_id": "S-A", "strategy_version": 1})
    write(root / "positions.json", [{"symbol": "600000.SH", "strategy_version": 1}])
    write(root / "strategy_versions" / "v1.json", {"version": 1, "status": "active"})
    write(root / "strategy_versions" / "v2.json", {"version": 2, "status": "candidate"})
    write(root / "strategy_evidence" / "v2" / "backtest.json", {"eligible": True, "reasons": []})

    result = strategies.approve_strategy("S-A", 2)

    assert result["activeVersion"] == 2
    assert json.loads((root / "positions.json").read_text(encoding="utf-8"))[0]["strategy_version"] == 1
    statuses = {item["version"]: item["status"] for item in result["versions"]}
    assert statuses == {1: "retired", 2: "active"}


def test_old_stored_active_version_is_reported_as_retired(monkeypatch, tmp_path):
    monkeypatch.setattr(strategies, "LEAGUE_DIR", tmp_path)
    root = tmp_path / "groups" / "S-A"
    write(root / "config.json", {"group_id": "S-A", "strategy_version": 2})
    write(root / "strategy_versions" / "v1.json", {"version": 1, "status": "active"})
    write(root / "strategy_versions" / "v2.json", {"version": 2, "status": "active"})

    result = strategies.strategy_versions("S-A")

    assert [item["status"] for item in result["versions"]] == ["retired", "active"]
