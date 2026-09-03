import json

from app.ai import archive_review, render_review_markdown


def sample_record():
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "createdAt": "2026-09-02T18:30:00+08:00",
        "trigger": "manual",
        "triggerType": "manual",
        "mode": "quick",
        "model": "deepseek-v4-flash",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        "inputGroups": ["S-A", "L-A"],
        "status": "candidate_only",
        "result": {
            "summary": "测试摘要",
            "winners": ["S-A"],
            "losers": ["L-A"],
            "selection_findings": ["选股证据"],
            "execution_findings": ["成交证据"],
            "external_findings": ["公告证据"],
            "main_force_findings": ["龙虎榜代理指标"],
            "candidate_changes": ["待回测调整"],
            "missing_information": ["无Level-2"],
            "risks": ["样本较少"],
        },
    }


def test_markdown_contains_classification_and_safety_boundary():
    markdown = render_review_markdown(sample_record())
    assert "# 手动快速复盘报告" in markdown
    assert "候选建议，尚未批准启用" in markdown
    assert "测试摘要" in markdown
    assert "龙虎榜代理指标" in markdown
    assert "无Level-2" in markdown


def test_archive_writes_json_and_markdown(tmp_path):
    archived = archive_review(sample_record(), tmp_path)
    json_path = tmp_path / archived["documents"]["json"]
    markdown_path = tmp_path / archived["documents"]["markdown"]
    assert json.loads(json_path.read_text(encoding="utf-8"))["mode"] == "quick"
    assert "手动快速复盘报告" in markdown_path.read_text(encoding="utf-8")
