import { useState } from "react";
import { api } from "../api";
import { AIBadge } from "../components/AIBadge";
import type { Group } from "../types";

type RunResult = { generatedAt: string; asOf: string; tradeDate: string; eligibleUniverse: number; groups: Array<{ groupId: string; status: string; count: number; notes: string[] }> };

function shanghaiToday() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

export function SelectionPage({ groups, aiReady, refresh }: { groups: Group[]; aiReady: boolean; refresh: () => Promise<void> | void }) {
  const [asOf, setAsOf] = useState(shanghaiToday());
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<RunResult>();
  const [error, setError] = useState("");
  const ready = groups.filter((group) => group.candidates.status === "ready");
  async function run() {
    setRunning(true); setError("");
    try {
      const result = await api<RunResult>("/api/selection/run", { method: "POST", body: JSON.stringify({ asOf }) });
      setRunResult(result);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "选股失败");
    } finally { setRunning(false); }
  }
  return <div className="page">
    <header className="page-header"><div><p className="eyebrow">规则驱动 · AI可选</p><h1>选股研究</h1><p>点击运行才会更新；本次结果持续保留到下一次主动运行。</p></div><div className="selection-run"><label>数据截止<input type="date" value={asOf} max={shanghaiToday()} onChange={(event) => setAsOf(event.target.value)} /></label><button className="primary" disabled={running || !asOf} onClick={run}>{running ? "正在计算…" : "运行八组选股"}</button></div></header>
    {error && <p className="service-error inline">选股失败：{error}</p>}
    {runResult && <p className="action-message">已按 {runResult.tradeDate} 行情完成：有效股票池 {runResult.eligibleUniverse} 只，{runResult.groups.filter((item) => item.status === "ready").length}/8 组生成候选。</p>}
    <section className="workflow"><div className="done"><b>1</b><span><strong>数据检查</strong><small>缺字段、ST、退市、科创板自动排除</small></span></div><i /><div className={ready.length ? "done" : "current"}><b>2</b><span><strong>规则选股</strong><small>{ready.length}/8 组已有可运行候选</small></span></div><i /><div><b>3</b><span><strong>等待入场</strong><small>按各组预期价格成交</small></span></div></section>
    <section className="notice"><div><strong>基础选股不需要 AI</strong><p>趋势、财务、流动性和风险门禁均由确定性规则计算；L-B 只统计截止日前近12个月已除息的现金分红，未来事件不计入。</p></div><AIBadge configured={aiReady} label="资讯研判" /></section>
    <section className="candidate-groups">
      {groups.map((group) => <article key={group.id}>
        <div className="candidate-title"><span className="group-code">{group.id}</span><div><strong>{group.name}</strong><small>{group.candidates.generated_at ? `生成于 ${new Date(group.candidates.generated_at).toLocaleString()}` : "尚未运行"}</small></div><em>{group.candidates.items?.length || 0} 只</em></div>
        <div className="candidate-table">
          {(group.candidates.items || []).slice(0, 5).map((item, index) => <div key={item.symbol}><span>{index + 1}</span><strong>{item.name}<small>{item.symbol}</small></strong><span>评分 {item.score.toFixed(1)}{item.pe_ttm != null && <small>PE TTM {item.pe_ttm.toFixed(2)}</small>}{item.valuation_source && <small>估值 {item.valuation_source} · {item.valuation_date}</small>}{item.dividend_yield != null && <small>近12月股息率 {(item.dividend_yield * 100).toFixed(2)}%</small>}</span><span>¥{item.close}</span><span>{item.entry_price_min && item.entry_price_max ? `入场 ${item.entry_price_min}–${item.entry_price_max}` : "待计算"}</span></div>)}
          {!group.candidates.items?.length && <div className="blocked-row"><strong>{group.candidates.status === "blocked" ? "数据门禁阻断" : "暂无候选"}</strong><p>{(group.candidates as { notes?: string[] }).notes?.join("；") || "没有满足完整门禁的候选，不会用缺失数据凑数。"}</p></div>}
        </div>
      </article>)}
    </section>
  </div>;
}
