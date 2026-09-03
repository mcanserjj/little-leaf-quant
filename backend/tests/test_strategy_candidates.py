import json

import app.strategies as strategies


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_ai_suggestion_creates_inactive_allowlisted_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(strategies, "LEAGUE_DIR", tmp_path)
    root = tmp_path / "groups" / "S-A"
    write(root / "config.json", {"strategy_version": 1})
    write(root / "strategy_versions" / "v1.json", {"group_id": "S-A", "version": 1, "status": "active", "horizon": "short", "parameters": {"stop_loss_pct": -.06}})
    review = {"id": "review-1", "result": {"candidate_changes": [{"group_id": "S-A", "reason": "test", "adjustments": [
        {"field": "stop_loss_pct", "current": -.06, "proposed": -.05, "evidence": "x", "risk": "y"},
        {"field": "parameters.unknown", "current": 1, "proposed": 2, "evidence": "x", "risk": "y"},
    ]}]}}

    result = strategies.materialize_review_candidates(review)
    candidate = json.loads((root / "strategy_versions" / "v2.json").read_text(encoding="utf-8"))

    assert result[0]["accepted"] == 1
    assert candidate["status"] == "candidate"
    assert candidate["parameters"]["stop_loss_pct"] == -.05
    assert candidate["source_review_id"] == "review-1"
    assert strategies.materialize_review_candidates(review)[0]["reused"] is True
