from __future__ import annotations

import json
import hashlib
import uuid
from pathlib import Path
from typing import Any

import httpx

from .config import AI_ARCHIVE_DIR, DEEPSEEK_BASE_URL, DEEPSEEK_DEEP_MODEL, DEEPSEEK_FAST_MODEL
from .secrets import load_deepseek_key
from .review_context import build_review_package
from .storage import now_iso, write_json
from .strategies import materialize_review_candidates

SYSTEM_PROMPT = """你是A股量化研究复盘助手。必须逐条遵守输入中的reviewRules，并综合账户、成交、选股、当前策略、数据覆盖、回测准备度、公司公告及公开龙虎榜/热榜/异动证据。只能引用输入包，不得承诺收益，不得虚构行情、财务、资讯、主力持仓或成交。公开资金数据只能称为代理指标。缺失信息必须明确列出。

输出JSON对象，字段必须包含：summary、winners、losers、selection_findings、execution_findings、external_findings、main_force_findings、candidate_changes、missing_information、risks。candidate_changes仅允许提出待回测调整；每项包含group_id、reason、adjustments，adjustments每项包含field、current、proposed、evidence、risk。不得宣称候选已回测、已批准或已启用。"""


def _trigger_type(trigger: str) -> str:
    return "automatic" if trigger.startswith("scheduled") else "manual"


def _category_labels(trigger: str, mode: str) -> tuple[str, str]:
    trigger_label = "每周自动" if _trigger_type(trigger) == "automatic" else "手动"
    mode_label = "深度" if mode == "deep" else "快速"
    return trigger_label, mode_label


def _markdown_lines(value: Any) -> list[str]:
    if value is None or value == "" or value == [] or value == {}:
        return ["暂无可验证内容。"]
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                text = "；".join(f"{key}：{val}" for key, val in item.items() if val not in (None, "", [], {}))
                lines.append(f"- {text or '暂无可验证内容'}")
            else:
                lines.append(f"- {item}")
        return lines or ["暂无可验证内容。"]
    if isinstance(value, dict):
        return [f"- {key}：{val}" for key, val in value.items()]
    return [str(value)]


def render_review_markdown(record: dict[str, Any]) -> str:
    result = record.get("result") or {}
    trigger_label, mode_label = _category_labels(record.get("trigger", "manual"), record.get("mode", "quick"))
    usage = record.get("usage") or {}
    sections = (
        ("结论摘要", result.get("summary")),
        ("胜出策略组", result.get("winners")),
        ("落后策略组", result.get("losers")),
        ("选股策略归因", result.get("selection_findings")),
        ("交易策略归因", result.get("execution_findings")),
        ("外部信息归因", result.get("external_findings")),
        ("主力动向代理指标", result.get("main_force_findings")),
        ("候选优化建议", result.get("candidate_changes")),
        ("未获取信息", result.get("missing_information") or record.get("missingInformation")),
        ("风险与限制", result.get("risks")),
    )
    lines = [
        f"# {trigger_label}{mode_label}复盘报告",
        "",
        "> 本报告由确定性账户与成交数据生成证据，再由 DeepSeek 解释。优化建议仅为待回测候选，不会自动修改运行策略。",
        "",
        "## 报告信息",
        "",
        f"- 生成时间：{record.get('createdAt', '-')}",
        f"- 触发方式：{trigger_label}",
        f"- 分析深度：{mode_label}",
        f"- 模型：{record.get('model', '-')}",
        f"- 策略组：{', '.join(record.get('inputGroups') or [])}",
        f"- 输入 Token：{usage.get('prompt_tokens', 0)}",
        f"- 输出 Token：{usage.get('completion_tokens', 0)}",
        f"- 总 Token：{usage.get('total_tokens', 0)}",
        f"- 策略状态：候选建议，尚未批准启用",
        f"- 复盘规则版本：{record.get('rulesHash', '-')}",
        f"- 回测准备：{record.get('backtestReadyGroups', 0)}/{record.get('backtestTotalGroups', 0)} 组存在可用样本",
    ]
    for title, value in sections:
        lines.extend(["", f"## {title}", "", *_markdown_lines(value)])
    lines.extend([
        "", "## 后续动作", "",
        "1. 对候选改动进行历史回测。",
        "2. 进行样本外和滚动窗口对照。",
        "3. 只有验证达标且经用户批准，才生成新的运行策略版本。",
        "",
    ])
    return "\n".join(lines)


def archive_review(record: dict[str, Any], root: Path = AI_ARCHIVE_DIR) -> dict[str, Any]:
    trigger_type = record["triggerType"]
    mode = record["mode"]
    day = record["createdAt"][:10]
    folder = root / trigger_type / mode / day[:4] / day[5:7]
    stem = f"{day}_{record['createdAt'][11:19].replace(':', '')}_{record['id']}"
    markdown = render_review_markdown(record)
    json_path = folder / f"{stem}.json"
    markdown_path = folder / f"{stem}.md"
    archived = {
        **record,
        "documents": {
            "json": json_path.relative_to(root).as_posix(),
            "markdown": markdown_path.relative_to(root).as_posix(),
        },
    }
    write_json(json_path, archived)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = markdown_path.with_suffix(".md.tmp")
    temporary.write_text(markdown, encoding="utf-8")
    temporary.replace(markdown_path)
    return archived


async def create_review(trigger: str, deep: bool = False) -> dict[str, Any]:
    key = load_deepseek_key()
    if not key:
        raise ValueError("请先配置DeepSeek API Key")
    evidence = build_review_package()
    mode = "deep" if deep else "quick"
    model = DEEPSEEK_DEEP_MODEL if deep else DEEPSEEK_FAST_MODEL
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"trigger": trigger, "mode": mode, **evidence}, ensure_ascii=False)},
        ],
        "thinking": {"type": "enabled" if deep else "disabled"},
        "reasoning_effort": "high" if deep else "low",
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": 3000,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        raw = response.json()
    result = json.loads(raw["choices"][0]["message"].get("content") or "{}")
    rules_hash = hashlib.sha256(json.dumps(evidence["reviewRules"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    record = {
        "id": str(uuid.uuid4()), "createdAt": now_iso(), "trigger": trigger,
        "triggerType": _trigger_type(trigger), "mode": mode, "model": raw.get("model", model),
        "usage": raw.get("usage", {}), "inputGroups": [group["id"] for group in evidence["groups"]],
        "rulesHash": rules_hash, "missingInformation": evidence["missingInformation"],
        "backtestReadyGroups": evidence["backtestReadiness"]["readyGroups"], "backtestTotalGroups": evidence["backtestReadiness"]["totalGroups"],
        "inputManifest": {"generatedAt": evidence["generatedAt"], "dataCoverage": evidence["dataCoverage"], "backtestReadiness": evidence["backtestReadiness"], "externalEvidenceCounts": {"announcements": len(evidence["externalEvidence"]["announcements"]), **{key: len(value) for key, value in evidence["externalEvidence"]["marketIntelligence"]["matched"].items()}}},
        "result": result, "status": "candidate_only",
    }
    record["materializedCandidates"] = materialize_review_candidates(record)
    return archive_review(record)


async def test_deepseek_connection(key: str | None = None) -> dict[str, Any]:
    token = (key or load_deepseek_key() or "").strip()
    if not token:
        raise ValueError("请先填写DeepSeek API Key")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{DEEPSEEK_BASE_URL}/models", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()
    return {"ok": True, "models": len(data.get("data") or [])}


def _review_path(review_id: str) -> Path | None:
    matches = list(AI_ARCHIVE_DIR.rglob(f"*_{review_id}.json"))
    if matches:
        return max(matches, key=lambda path: len(path.parts))
    for path in AI_ARCHIVE_DIR.glob("*.json"):
        item = _read_record(path)
        if item and item.get("id") == review_id:
            return path
    return None


def migrate_legacy_reviews() -> int:
    migrated = 0
    for path in AI_ARCHIVE_DIR.glob("*.json"):
        item = _read_record(path)
        if not item or not item.get("id"):
            continue
        trigger = item.get("trigger", "manual")
        mode = item.get("mode", "deep" if item.get("model") == DEEPSEEK_DEEP_MODEL else "quick")
        enriched = {**item, "triggerType": item.get("triggerType", _trigger_type(trigger)), "mode": mode}
        expected = AI_ARCHIVE_DIR / enriched["triggerType"] / mode
        if not any(expected.rglob(f"*_{item['id']}.json")):
            archive_review(enriched)
            migrated += 1
    return migrated


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_review(review_id: str) -> dict[str, Any] | None:
    path = _review_path(review_id)
    if not path:
        return None
    record = _read_record(path)
    if not record:
        return None
    markdown_path = path.with_suffix(".md")
    markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else render_review_markdown(record)
    return {**record, "markdown": markdown}


def list_reviews() -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for path in AI_ARCHIVE_DIR.rglob("*.json"):
        item = _read_record(path)
        if not item or not item.get("id") or item["id"] in seen:
            continue
        preferred = _review_path(item["id"])
        if preferred and preferred != path:
            continue
        seen.add(item["id"])
        trigger = item.get("trigger", "manual")
        mode = item.get("mode", "deep" if item.get("model") == DEEPSEEK_DEEP_MODEL else "quick")
        trigger_label, mode_label = _category_labels(trigger, mode)
        rows.append({
            "id": item.get("id"), "createdAt": item.get("createdAt"), "trigger": trigger,
            "triggerType": item.get("triggerType", _trigger_type(trigger)), "triggerLabel": trigger_label,
            "mode": mode, "modeLabel": mode_label, "model": item.get("model"),
            "usage": item.get("usage", {}), "status": item.get("status", "candidate_only"),
            "summary": (item.get("result") or {}).get("summary"),
        })
    return sorted(rows, key=lambda item: item.get("createdAt", ""), reverse=True)
