from __future__ import annotations

import copy
from typing import Any

from .config import LEAGUE_DIR
from .selection import DEFAULT_SELECTION_PARAMETERS
from .storage import GROUP_IDS, now_iso, read_json, write_json


ALLOWED_AI_FIELDS = {
    "parameters.stop_loss_pct": (-.20, -.01),
    "parameters.take_profit_pct": (.02, .60),
    "parameters.max_holding_days": (2, 250),
    "parameters.entry_price.lower_vol_multiplier": (-3, 0),
    "parameters.entry_price.upper_vol_multiplier": (0, 3),
    "parameters.selection.min_avg_amount20": (10_000_000, 500_000_000),
    "parameters.selection.min_return20": (0, .30),
    "parameters.selection.max_volatility60": (.20, 1.50),
}
WEIGHT_SUFFIXES = {
    "momentum_weight", "liquidity_weight", "low_vol_weight", "reversal_weight", "event_weight",
    "roe_delta_weight", "roe_weight", "low_debt_weight",
}


def _normal_field(value: str) -> str:
    field = value.strip()
    if not field.startswith("parameters."):
        field = "parameters." + field
    return field


def _set_nested(document: dict[str, Any], field: str, value: Any) -> None:
    parts = field.split(".")
    target = document
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def materialize_review_candidates(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn allow-listed AI suggestions into inactive candidate files; never approve them."""
    review_id = str(review.get("id") or "")
    changes = (review.get("result") or {}).get("candidate_changes") or []
    created: list[dict[str, Any]] = []
    for change in changes:
        group_id = str(change.get("group_id") or "")
        if group_id not in GROUP_IDS:
            continue
        root = LEAGUE_DIR / "groups" / group_id
        for path in (root / "strategy_versions").glob("v*.json"):
            existing = read_json(path, {})
            if existing.get("source_review_id") == review_id:
                created.append({"groupId": group_id, "version": existing.get("version"), "reused": True})
                break
        else:
            config = read_json(root / "config.json", {})
            active_version = int(config.get("strategy_version", 1))
            active = read_json(root / "strategy_versions" / f"v{active_version}.json", {})
            if not active:
                continue
            candidate = copy.deepcopy(active)
            parameters = candidate.setdefault("parameters", {})
            parameters["selection"] = {**DEFAULT_SELECTION_PARAMETERS[group_id], **(parameters.get("selection") or {})}
            accepted = []
            rejected = []
            for adjustment in change.get("adjustments") or []:
                field = _normal_field(str(adjustment.get("field") or ""))
                try:
                    proposed = float(adjustment.get("proposed"))
                except (TypeError, ValueError):
                    rejected.append({"field": field, "reason": "proposed不是数值"})
                    continue
                bounds = ALLOWED_AI_FIELDS.get(field)
                if field.startswith("parameters.selection.") and field.rsplit(".", 1)[-1] in WEIGHT_SUFFIXES:
                    bounds = (0, 100)
                if not bounds or not bounds[0] <= proposed <= bounds[1]:
                    rejected.append({"field": field, "reason": "字段未获准或超出安全范围"})
                    continue
                if field == "parameters.max_holding_days":
                    proposed = int(proposed)
                    maximum = 30 if candidate.get("horizon") == "short" else 250
                    if proposed > maximum:
                        rejected.append({"field": field, "reason": "持有期超出该周期上限"})
                        continue
                _set_nested(candidate, field, proposed)
                accepted.append({**adjustment, "field": field, "proposed": proposed})
            selection = candidate["parameters"]["selection"]
            weight_keys = [key for key in selection if key in WEIGHT_SUFFIXES]
            if any(item["field"].rsplit(".", 1)[-1] in WEIGHT_SUFFIXES for item in accepted) and abs(sum(float(selection[key]) for key in weight_keys) - 100) > .001:
                rejected.extend({"field": item["field"], "reason": "同组权重合计必须为100"} for item in accepted if item["field"].rsplit(".", 1)[-1] in WEIGHT_SUFFIXES)
                candidate["parameters"]["selection"] = {**DEFAULT_SELECTION_PARAMETERS[group_id], **((active.get("parameters") or {}).get("selection") or {})}
                accepted = [item for item in accepted if item["field"].rsplit(".", 1)[-1] not in WEIGHT_SUFFIXES]
            if not accepted:
                continue
            versions = [int(path.stem[1:]) for path in (root / "strategy_versions").glob("v*.json")]
            version = max(versions, default=0) + 1
            candidate.update({
                "version": version, "parent_version": active_version, "status": "candidate",
                "created_at": now_iso(), "source_review_id": review_id,
                "change_reason": str(change.get("reason") or "DeepSeek复盘生成的待回测候选"),
                "ai_adjustments": accepted, "rejected_ai_adjustments": rejected,
            })
            candidate.pop("approved_at", None)
            write_json(root / "strategy_versions" / f"v{version}.json", candidate)
            created.append({"groupId": group_id, "version": version, "accepted": len(accepted), "rejected": len(rejected)})
    return created


def strategy_versions(group_id: str) -> dict[str, Any]:
    if group_id not in GROUP_IDS:
        raise ValueError("未知策略组")
    root = LEAGUE_DIR / "groups" / group_id
    config = read_json(root / "config.json", {})
    active = int(config.get("strategy_version", 1))
    versions = []
    for path in sorted((root / "strategy_versions").glob("v*.json"), key=lambda item: int(item.stem[1:])):
        item = read_json(path, {})
        number = int(item.get("version", 0))
        evidence = read_json(root / "strategy_evidence" / f"v{number}" / "backtest.json", {})
        stored_status = item.get("status", "retired")
        status = "active" if number == active else "retired" if stored_status == "active" else stored_status
        versions.append({**item, "status": status, "backtest": evidence, "approvable": number != active and stored_status in {"candidate", "pending_approval"} and evidence.get("eligible") is True})
    return {"groupId": group_id, "activeVersion": active, "versions": versions}


def all_strategy_versions() -> list[dict[str, Any]]:
    return [strategy_versions(group_id) for group_id in GROUP_IDS]


def approve_strategy(group_id: str, version: int) -> dict[str, Any]:
    state = strategy_versions(group_id)
    candidate = next((item for item in state["versions"] if int(item.get("version", 0)) == version), None)
    if not candidate:
        raise ValueError("候选策略版本不存在")
    if not candidate.get("approvable"):
        reasons = (candidate.get("backtest") or {}).get("reasons") or ["尚未完成合格回测"]
        raise ValueError("不能批准：" + "；".join(str(reason) for reason in reasons))
    root = LEAGUE_DIR / "groups" / group_id
    config = read_json(root / "config.json", {})
    previous = int(config.get("strategy_version", 1))
    config.update({"strategy_version": version, "status": "active", "approved_at": now_iso(), "previous_strategy_version": previous, "new_positions_only": True})
    write_json(root / "config.json", config)
    candidate.update({"status": "active", "approved_at": now_iso(), "approved_from_new_positions_only": True})
    candidate.pop("backtest", None)
    candidate.pop("approvable", None)
    write_json(root / "strategy_versions" / f"v{version}.json", candidate)
    return strategy_versions(group_id)
