import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { AIBadge } from "../components/AIBadge";
import type { Group } from "../types";

type ReviewSummary = {
  id: string; createdAt: string; triggerType: "manual" | "automatic";
  triggerLabel: string; mode: "quick" | "deep"; modeLabel: string;
  model: string; status: string; summary?: unknown;
  usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
};
type ReviewDocument = ReviewSummary & { markdown: string; result: Record<string, unknown>; documents?: { json: string; markdown: string } };
type BacktestMetrics = { totalReturnPct?: number; maxDrawdownPct?: number; sharpe?: number; trades?: number };
type StrategyVersion = { version: number; status: string; change_reason?: string; approvable?: boolean; backtest?: { reasons?: string[]; eligible?: boolean; generatedAt?: string; cases?: Record<string, { outOfSample?: BacktestMetrics }> } };
type StrategyState = { groupId: string; activeVersion: number; versions: StrategyVersion[] };
type Readiness = { readyGroups: number; totalGroups: number; policy: string; groups: Array<{ groupId: string; coveragePct: number; includedSymbols: unknown[]; excludedSymbols: unknown[]; strategyDataReady?: boolean; strategyDataReasons?: string[] }> };
type BacktestStatus = { state: string; completed?: number; total?: number; current?: string; updatedAt?: string; error?: string };
type ScheduleStatus = { state: string; target?: string; lastCompletedTarget?: string; error?: string };

function summaryText(value: unknown) {
  if (typeof value === "string") return value;
  if (value == null) return "暂无摘要";
  return JSON.stringify(value, null, 2);
}

export function LeaguePage({ groups, aiReady }: { groups: Group[]; aiReady: boolean }) {
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const [reviews, setReviews] = useState<ReviewSummary[]>([]);
  const [selected, setSelected] = useState<ReviewDocument>();
  const [triggerFilter, setTriggerFilter] = useState<"all" | "manual" | "automatic">("all");
  const [modeFilter, setModeFilter] = useState<"all" | "quick" | "deep">("all");
  const [format, setFormat] = useState<"markdown" | "json">("markdown");
  const [strategies, setStrategies] = useState<StrategyState[]>([]);
  const [readiness, setReadiness] = useState<Readiness>();
  const [backtest, setBacktest] = useState<BacktestStatus>({ state: "idle" });
  const [schedule, setSchedule] = useState<ScheduleStatus>({ state: "idle" });
  const sorted = [...groups].sort((a, b) => b.returnPct - a.returnPct);
  const visibleReviews = useMemo(() => reviews.filter((review) =>
    (triggerFilter === "all" || review.triggerType === triggerFilter) &&
    (modeFilter === "all" || review.mode === modeFilter)
  ), [reviews, triggerFilter, modeFilter]);

  async function loadReviews(preferredId?: string) {
    const rows = await api<ReviewSummary[]>("/api/reviews");
    setReviews(rows);
    const id = preferredId || selected?.id || rows[0]?.id;
    if (id) setSelected(await api<ReviewDocument>(`/api/reviews/${id}`));
  }
  async function loadGovernance() {
    const [strategyRows, readinessRow, backtestRow, scheduleRow] = await Promise.all([api<StrategyState[]>("/api/strategies"), api<Readiness>("/api/backtest/readiness"), api<BacktestStatus>("/api/backtest/status"), api<ScheduleStatus>("/api/reviews/schedule/status")]);
    setStrategies(strategyRows); setReadiness(readinessRow); setBacktest(backtestRow); setSchedule(scheduleRow);
  }
  useEffect(() => { loadReviews().catch(() => undefined); loadGovernance().catch(() => undefined); }, []);
  useEffect(() => { const timer = window.setInterval(() => { if (["starting", "running"].includes(backtest.state)) loadGovernance().catch(() => undefined); }, 2000); return () => window.clearInterval(timer); }, [backtest.state]);
  useEffect(() => {
    if (selected && visibleReviews.some((review) => review.id === selected.id)) return;
    const next = visibleReviews[0];
    if (!next) {
      setSelected(undefined);
      return;
    }
    api<ReviewDocument>(`/api/reviews/${next.id}`).then(setSelected).catch(() => setSelected(undefined));
  }, [visibleReviews, selected?.id]);

  async function review(deep: boolean) {
    setWorking(true); setMessage("");
    try {
      const created = await api<ReviewDocument>("/api/reviews", { method: "POST", body: JSON.stringify({ deep }) });
      setMessage(`${deep ? "深度" : "快速"}复盘已生成 JSON 与 Markdown，并归档为待验证建议。`);
      await loadReviews(created.id);
    } catch (error) { setMessage(error instanceof Error ? error.message : "复盘失败"); }
    finally { setWorking(false); }
  }
  async function approve(groupId: string, version: number) {
    if (!window.confirm(`批准 ${groupId} v${version}？批准后立即成为新开仓使用的版本，已有仓位保持原版本。`)) return;
    try { await api(`/api/strategies/${groupId}/versions/${version}/approve`, { method: "POST" }); setMessage(`${groupId} v${version} 已批准，仅影响后续新开仓。`); await loadGovernance(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "批准失败"); }
  }
  async function runBacktest() {
    try { await api("/api/backtest/run", { method: "POST" }); setMessage("已在后台启动全版本真实历史回测。"); setBacktest({ state: "starting" }); }
    catch (error) { setMessage(error instanceof Error ? error.message : "回测启动失败"); }
  }

  return <div className="page">
    <header className="page-header"><div><p className="eyebrow">周赛 · 归因 · 策略进化</p><h1>联赛与复盘</h1><p>系统先计算事实，AI只负责解释和提出待验证建议。</p></div><AIBadge configured={aiReady} label="复盘解说" /></header>
    <section className="review-actions"><div><strong>每周自动复盘</strong><p>交易周周五 16:30；遇休市顺延至下一实际交易日收盘后 · 状态：{schedule.state}{schedule.error ? ` · ${schedule.error}` : ""}</p></div><button className="secondary" disabled={!aiReady || working} onClick={() => review(false)}>AI 快速复盘</button><button className="primary" disabled={!aiReady || working} onClick={() => review(true)}>AI 深度复盘</button></section>
    {message && <p className="action-message">{message}</p>}
    <section className="league-layout"><article className="ranking"><div className="panel-title"><h2>本周排名</h2><span>按当前净值收益</span></div>{sorted.map((group, index) => <div className="rank-row" key={group.id}><b>{index + 1}</b><span className="group-code">{group.id}</span><strong>{group.name}</strong><em className={group.returnPct >= 0 ? "up" : "down"}>{group.returnPct >= 0 ? "+" : ""}{group.returnPct.toFixed(2)}%</em></div>)}</article>
      <article className="review-explainer"><div className="panel-title"><h2>复盘如何生效</h2><span>不会静默改策略</span></div><ol><li><b>规则与证据</b><p>将复盘规则、当前策略、可用数据和缺失项一起传给 DeepSeek。</p></li><li><b>AI 解释与建议</b><p>结合公告及公开龙虎榜/热榜/异动，缺失信息必须明示。</p></li><li><b>先准备再回测</b><p>缺失股票或时间段排除，不填充、不估算，并报告覆盖率。</p></li><li><b>人工批准</b><p>合格候选批准后立即供新开仓使用；已有仓位保留原版本。</p></li></ol></article></section>

    <section className="governance-section"><div className="panel-title"><h2>策略版本与人工批准</h2><span>当前生效版本始终可见</span></div><div className="backtest-control"><div><strong>历史回测证据</strong><small>{["starting", "running"].includes(backtest.state) ? `${backtest.current || "准备数据"} · ${backtest.completed || 0}/${backtest.total || 0}` : backtest.state === "completed" ? `已完成 ${backtest.completed || 0}/${backtest.total || 0}` : backtest.error || "尚未运行"}</small></div><button className="secondary" disabled={["starting", "running"].includes(backtest.state)} onClick={runBacktest}>{["starting", "running"].includes(backtest.state) ? "回测中" : "运行全版本回测"}</button></div><div className="strategy-version-grid">{strategies.map((state) => { const candidates = state.versions.filter((version) => version.status === "candidate" || version.status === "pending_approval"); const active = state.versions.find((version) => version.version === state.activeVersion); const activeOos = active?.backtest?.cases?.["current_snapshot:5bps"]?.outOfSample; const hasMetrics = activeOos?.totalReturnPct != null; return <article key={state.groupId}><div><span className="group-code">{state.groupId}</span><strong>当前 v{state.activeVersion}</strong></div>{hasMetrics ? <small>样本外 {activeOos?.totalReturnPct?.toFixed(2)}% · 回撤 {activeOos?.maxDrawdownPct?.toFixed(2)}% · {activeOos?.trades || 0}笔</small> : active?.backtest?.generatedAt && <small>回测不可用：{active.backtest.reasons?.[0] || "关键字段不足"}</small>}{candidates.length ? candidates.map((version) => <div className="candidate-version" key={version.version}><span>候选 v{version.version}</span><small>{version.backtest?.eligible ? "回测门禁通过" : version.backtest?.reasons?.join("；") || "等待回测"}</small><button className="primary" disabled={!version.approvable} onClick={() => approve(state.groupId, version.version)}>批准启用</button></div>) : <small>暂无待批准候选</small>}</article>; })}</div></section>

    <section className="readiness-panel"><div className="panel-title"><h2>回测数据准备</h2><span>{readiness?.readyGroups || 0}/{readiness?.totalGroups || 8} 组具备完整策略字段</span></div><p>{readiness?.policy || "正在检查数据覆盖…"}</p><div>{readiness?.groups.map((group) => <span key={group.groupId} title={group.strategyDataReasons?.join("；")}><b>{group.groupId}</b> {group.strategyDataReady ? `基础样本 ${group.coveragePct.toFixed(0)}%` : `缺口：${group.strategyDataReasons?.[0] || "未知"}`}</span>)}</div></section>

    <section className="archive-section">
      <div className="archive-heading"><div><p className="eyebrow">复盘档案</p><h2>历史文档</h2></div><div className="archive-filters"><div className="segmented"><button className={triggerFilter === "all" ? "active" : ""} onClick={() => setTriggerFilter("all")}>全部触发</button><button className={triggerFilter === "manual" ? "active" : ""} onClick={() => setTriggerFilter("manual")}>手动</button><button className={triggerFilter === "automatic" ? "active" : ""} onClick={() => setTriggerFilter("automatic")}>每周自动</button></div><div className="segmented"><button className={modeFilter === "all" ? "active" : ""} onClick={() => setModeFilter("all")}>全部深度</button><button className={modeFilter === "quick" ? "active" : ""} onClick={() => setModeFilter("quick")}>快速</button><button className={modeFilter === "deep" ? "active" : ""} onClick={() => setModeFilter("deep")}>深度</button></div></div></div>
      <div className="archive-browser">
        <aside className="archive-list">{visibleReviews.map((review) => <button key={review.id} className={selected?.id === review.id ? "active" : ""} onClick={async () => setSelected(await api<ReviewDocument>(`/api/reviews/${review.id}`))}><span><b>{review.triggerLabel}</b><b>{review.modeLabel}</b></span><strong>{new Date(review.createdAt).toLocaleString()}</strong><p>{summaryText(review.summary)}</p><small>{review.model} · {review.usage?.total_tokens || 0} tokens</small></button>)}{!visibleReviews.length && <div className="archive-empty"><strong>还没有符合条件的复盘</strong><p>配置 DeepSeek 后，可手动生成快速或深度复盘；每周自动复盘也会进入这里。</p></div>}</aside>
        <article className="document-viewer">{selected ? <><div className="document-toolbar"><div><strong>{selected.triggerLabel}{selected.modeLabel}复盘</strong><small>{new Date(selected.createdAt).toLocaleString()} · {selected.model}</small></div><div className="segmented"><button className={format === "markdown" ? "active" : ""} onClick={() => setFormat("markdown")}>阅读文档</button><button className={format === "json" ? "active" : ""} onClick={() => setFormat("json")}>结构数据</button></div></div><pre>{format === "markdown" ? selected.markdown : JSON.stringify(selected.result, null, 2)}</pre></> : <div className="document-placeholder"><strong>选择一份复盘文档</strong><p>这里可以翻看手动/自动、快速/深度复盘的 Markdown 与 JSON。</p></div>}</article>
      </div>
    </section>
  </div>;
}
