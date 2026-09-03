from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .config import DATA_DIR, LEAGUE_DIR, NEWS_DIR
from .storage import all_groups, data_coverage, now_iso, read_json

REVIEW_RULES = {
    "evidence": [
        "只引用输入包中的行情、成交、财务、公告、龙虎榜、热榜与异动数据",
        "缺失信息必须写入missing_information，不得推断、补零或编造",
        "区分事实、模型解释与待验证假设，并注明来源和时间",
        "公开龙虎榜、热榜和异动只能作为资金/情绪代理，不能表述为完整主力持仓",
    ],
    "attribution": [
        "分别归因选股、入场、退出、仓位、费用、滑点、市场环境和事件冲击",
        "赢家与输家必须指出对应规则和可核验数据，不能只按收益率下结论",
        "短线组与长线组分别比较，不把不同持有周期直接混排为策略优劣",
    ],
    "strategy_change": [
        "只能提出待回测候选，不得宣称已启用",
        "参数改动必须给出group_id、字段、当前值、建议值、依据和风险",
        "候选必须先完成数据准备、样本内、样本外、滚动与成本敏感性验证，再允许人工批准",
        "批准后只影响新开仓；已有仓位继续沿用其买入时记录的策略版本",
    ],
    "market_rules": ["A股T+1", "100股整数倍", "停牌不成交", "涨跌停不可成交", "计入佣金、印花税、过户费和滑点"],
}


def backtest_readiness(groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = groups or all_groups()
    symbols_by_group = {
        group["id"]: sorted({
            *(str(item.get("symbol")) for item in group.get("positions", []) if item.get("symbol")),
            *(str(item.get("symbol")) for item in (group.get("candidates") or {}).get("items", []) if item.get("symbol")),
        })
        for group in rows
    }
    all_symbols = sorted({symbol for symbols in symbols_by_group.values() for symbol in symbols})
    bar_counts: dict[str, int] = {}
    paths = list((DATA_DIR / "kline_daily_enriched").glob("date=*/part.parquet"))
    if paths and all_symbols:
        frame = pl.scan_parquet([str(path) for path in paths]).filter(pl.col("symbol").is_in(all_symbols)).group_by("symbol").len().collect()
        bar_counts = dict(zip(frame["symbol"].to_list(), frame["len"].to_list(), strict=False))
    metrics_path = DATA_DIR / "financials" / "metrics" / "part.parquet"
    financial_symbols = set()
    if metrics_path.exists():
        financial_symbols = set(pl.read_parquet(metrics_path, columns=["symbol", "roe"]).filter(pl.col("roe").is_not_null())["symbol"].to_list())
    group_rows = []
    for group in rows:
        minimum = 60 if group.get("horizon") == "short" else 120
        included, excluded = [], []
        for symbol in symbols_by_group[group["id"]]:
            reasons = []
            if bar_counts.get(symbol, 0) < minimum:
                reasons.append(f"复权日K少于{minimum}条")
            if group.get("horizon") == "long" and symbol not in financial_symbols:
                reasons.append("缺少可用最新ROE")
            (excluded if reasons else included).append({"symbol": symbol, "reasons": reasons, "bars": bar_counts.get(symbol, 0)})
        version = int((group.get("strategy") or {}).get("version") or 1)
        evidence = read_json(LEAGUE_DIR / "groups" / group["id"] / "strategy_evidence" / f"v{version}" / "backtest.json", {})
        data_case = (((evidence.get("cases") or {}).get("current_snapshot:5bps") or {}).get("outOfSample") or {})
        strategy_data_ready = bool(data_case.get("eligibleData")) if evidence else bool(included)
        data_reasons = [] if strategy_data_ready else (data_case.get("reasons") or evidence.get("reasons") or ["尚未生成策略级回测证据"])
        group_rows.append({"groupId": group["id"], "minimumBars": minimum, "includedSymbols": included, "excludedSymbols": excluded, "coveragePct": round(len(included) / len(symbols_by_group[group["id"]]) * 100, 2) if symbols_by_group[group["id"]] else 0, "strategyDataReady": strategy_data_ready, "strategyDataReasons": data_reasons})
    return {
        "generatedAt": now_iso(),
        "policy": "缺失股票或时间段直接排除，不填充、不估算；其余有效样本继续回测",
        "groups": group_rows,
        "readyGroups": sum(bool(row["strategyDataReady"]) for row in group_rows),
        "totalGroups": len(group_rows),
    }


def _external_evidence(groups: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    symbols = {str(item.get("symbol")) for group in groups for item in [*group.get("positions", []), *((group.get("candidates") or {}).get("items", []))] if item.get("symbol")}
    announcements = [item for item in read_json(NEWS_DIR / "events.json", []) if item.get("symbol") in symbols]
    intelligence = read_json(NEWS_DIR / "market-intelligence-latest.json", {})
    matched: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for category in ("dragonTiger", "hotStocks", "anomalies"):
        data = intelligence.get(category) or {}
        source_rows = data.get("stock_items") or data.get("item") or []
        for item in source_rows:
            symbol = item.get("thscode")
            if symbol in symbols:
                matched[category].append(item)
    missing = []
    if not announcements:
        missing.append("当前持仓/候选未匹配到可追溯公司公告")
    if not intelligence:
        missing.append("尚未获取HiThink龙虎榜、热榜与当日异动")
    elif intelligence.get("failures"):
        missing.extend(f"{item.get('dataset')}获取失败：{item.get('reason')}" for item in intelligence["failures"])
    missing.extend(["未接入Level-2逐笔与盘口", "公开数据无法证明完整主力持仓", "HiThink公开能力不提供新闻公告原文和研报原文"])
    return {
        "announcements": announcements,
        "marketIntelligence": {"source": intelligence.get("source"), "retrievedAt": intelligence.get("retrievedAt"), "matched": dict(matched), "limitations": intelligence.get("limitations", [])},
    }, missing


def build_review_package() -> dict[str, Any]:
    groups = all_groups()
    external, missing = _external_evidence(groups)
    coverage = data_coverage()
    for item in coverage:
        if not item.get("available"):
            missing.append(f"{item.get('label')}不可用")
        elif item.get("completeCompanies") is not None and item.get("totalCompanies") and item["completeCompanies"] < item["totalCompanies"]:
            missing.append(f"{item.get('label')}覆盖 {item['completeCompanies']}/{item['totalCompanies']} 家")
    readiness = backtest_readiness(groups)
    return {
        "generatedAt": now_iso(),
        "reviewRules": REVIEW_RULES,
        "groups": [{"id": group["id"], "name": group["name"], "horizon": group["horizon"], "returnPct": group["returnPct"], "account": group["account"], "positions": group["positions"], "trades": group["trades"], "currentStrategy": group["strategy"], "candidates": group["candidates"]} for group in groups],
        "dataCoverage": coverage,
        "backtestReadiness": readiness,
        "externalEvidence": external,
        "missingInformation": sorted(set(missing)),
    }
