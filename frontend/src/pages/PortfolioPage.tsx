import { useMemo, useState } from "react";
import { api } from "../api";
import type { Group, Overview } from "../types";

function money(value: unknown) {
  return `¥${Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
}

export function PortfolioPage({ groups, overview, refresh }: { groups: Group[]; overview?: Overview; refresh: () => void }) {
  const [horizon, setHorizon] = useState<"short" | "long">("short");
  const [openGroup, setOpenGroup] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const visible = useMemo(() => groups.filter((group) => group.horizon === horizon), [groups, horizon]);
  const execution = overview?.execution;
  async function refreshMarket() {
    setWorking(true); setMessage("");
    try { const result = await api<{ reason?: string }>("/api/execution/refresh", { method: "POST" }); setMessage(result.reason || "行情状态已刷新"); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "刷新失败"); }
    finally { setWorking(false); }
  }
  async function setMode(mode: "current_snapshot" | "next_snapshot") {
    setWorking(true); setMessage("");
    try { await api("/api/execution/mode", { method: "PUT", body: JSON.stringify({ mode }) }); setMessage("成交方式已更新，将用于后续新信号。"); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "设置失败"); }
    finally { setWorking(false); }
  }
  return (
    <div className="page">
      <header className="page-header">
        <div><p className="eyebrow">模拟账户总览</p><h1>五仓模拟</h1><p>先看哪组需要关注，再展开持仓细节。</p></div>
        <button className="secondary" disabled={working} onClick={refreshMarket}>↻ 刷新行情</button>
      </header>
      {message && <p className="action-message">{message}</p>}
      <section className={`execution-strip ${execution?.state || "waiting"}`}>
        <div><span>自动模拟执行</span><strong>{execution?.state === "rate_limited" ? "限流冷却" : execution?.enabled ? "已启用" : "已暂停"}</strong><small>{execution?.reason || "等待服务状态"}</small></div>
        <div><span>行情刷新</span><strong>{execution?.quoteRefreshSeconds || 5} 秒</strong><small>{execution?.lastQuoteAt ? `最近 ${new Date(execution.lastQuoteAt).toLocaleTimeString()} · ${execution.quoteScope || "行情"}` : "尚无在线行情"}</small></div>
        <div><span>策略评估</span><strong>{Math.round((execution?.strategyEvaluationSeconds || 600) / 60)} 分钟</strong><small>{execution?.lastEvaluationAt ? `最近 ${new Date(execution.lastEvaluationAt).toLocaleTimeString()}` : "尚未自动评估"}</small></div>
        <label><span>成交方式</span><select value={execution?.executionMode || "current_snapshot"} disabled={working} onChange={(event) => setMode(event.target.value as "current_snapshot" | "next_snapshot")}><option value="current_snapshot">信号本轮现价</option><option value="next_snapshot">下一快照成交</option></select><small>复盘会保留成交方式</small></label>
      </section>
      <details className="risk-panel"><summary>风险检查与运行边界</summary><p>仅在已公布交易日历和连续竞价时段运行；缺少 HiThink Key、行情字段、成交额或候选策略版本不一致时不成交。新增股票继续排除 ST、退市、科创板、创业板和北交所；卖出遵守 T+1 与涨跌停限制。</p></details>
      <section className="summary-grid">
        <article><span>策略组</span><strong>{overview?.groups ?? 8}</strong><small>短线4组 · 长线4组</small></article>
        <article><span>持仓数量</span><strong>{overview?.positions ?? 0}</strong><small>每组最多5仓</small></article>
        <article><span>平均收益</span><strong className={(overview?.averageReturnPct || 0) >= 0 ? "up" : "down"}>{(overview?.averageReturnPct || 0).toFixed(2)}%</strong><small>按八组净值计算</small></article>
        <article><span>行情快照</span><strong>{overview?.quoteCount || 0}</strong><small>{overview?.lastQuoteAt ? new Date(overview.lastQuoteAt).toLocaleString() : "尚无记录"}</small></article>
      </section>
      <div className="section-toolbar">
        <div className="segmented"><button className={horizon === "short" ? "active" : ""} onClick={() => setHorizon("short")}>短线组 S</button><button className={horizon === "long" ? "active" : ""} onClick={() => setHorizon("long")}>长线组 L</button></div>
        <span className="hint">点击策略组查看五个仓位与策略详情</span>
      </div>
      <section className="group-list">
        {visible.map((group) => {
          const open = openGroup === group.id;
          return <article className={`group-card ${open ? "open" : ""}`} key={group.id}>
            <button className="group-summary" onClick={() => setOpenGroup(open ? null : group.id)}>
              <span className="group-code">{group.id}</span>
              <span className="group-name"><strong>{group.name}</strong><small>{group.positions.length}/5 仓 · 策略 v{String(group.strategy.version || "-")}</small></span>
              <span className="nav-value"><small>净值</small><strong>{money(group.account.nav)}</strong></span>
              <span className={group.returnPct >= 0 ? "return up" : "return down"}>{group.returnPct >= 0 ? "+" : ""}{group.returnPct.toFixed(2)}%</span>
              <span className="chevron">{open ? "⌃" : "⌄"}</span>
            </button>
            {open && <div className="group-detail">
              <div className="detail-heading"><h3>当前持仓</h3><span>现价跟随行情服务更新；暂无现价时不伪造</span></div>
              <div className="position-grid">
                {Array.from({ length: 5 }).map((_, index) => {
                  const position = group.positions[index];
                  return <div className={`position-slot ${position ? "filled" : "empty"}`} key={index}>
                    <span className="slot-number">0{index + 1}</span>
                    {position ? <><strong>{position.name}</strong><small>{position.symbol} · 策略 v{position.strategy_version || "-"}</small><dl><div><dt>现价</dt><dd>{position.current_price ? money(position.current_price) : "待行情"}</dd></div><div><dt>买入价</dt><dd>{money(position.buy_price ?? position.average_cost)}</dd></div><div><dt>浮动盈亏</dt><dd className={(position.unrealized_pnl || 0) >= 0 ? "up" : "down"}>{position.unrealized_pnl == null ? "待行情" : `${money(position.unrealized_pnl)} / ${(position.return_pct || 0).toFixed(2)}%`}</dd></div><div><dt>数量</dt><dd>{position.quantity}</dd></div><div><dt>买入时间</dt><dd>{position.buy_time ? new Date(position.buy_time).toLocaleString() : "-"}</dd></div></dl></> : <><strong>等待信号</strong><small>允许空仓，不强制买入</small></>}
                  </div>;
                })}
              </div>
              <details className="history-panel"><summary>历史成交（{group.trades.length}）</summary>{group.trades.length ? <div className="trade-table"><div className="trade-head"><span>股票</span><span>买入时间 / 价格</span><span>卖出时间 / 价格</span><span>盈亏</span><span>卖出原因</span></div>{group.trades.slice().reverse().map((trade, index) => <div key={String(trade.trade_id || index)}><span>{String(trade.name || trade.symbol || "-")}<small>{String(trade.symbol || "")}</small></span><span>{trade.buy_time ? new Date(String(trade.buy_time)).toLocaleString() : "-"}<small>{money(trade.buy_price)}</small></span><span>{trade.sell_time ? new Date(String(trade.sell_time)).toLocaleString() : "-"}<small>{money(trade.sell_price)}</small></span><span className={Number(trade.realized_pnl || 0) >= 0 ? "up" : "down"}>{money(trade.realized_pnl)}<small>{Number(trade.return_pct || 0).toFixed(2)}%</small></span><span>{String(trade.sell_reason || "-")}</span></div>)}</div> : <p className="empty-text">暂无已完成卖出成交。</p>}</details>
              <details className="history-panel"><summary>最近策略判断（{group.decisions.length}）</summary>{group.decisions.length ? <ul className="decision-list">{group.decisions.slice().reverse().map((decision, index) => <li key={index}><time>{decision.time ? new Date(String(decision.time)).toLocaleString() : "-"}</time><strong>{String(decision.symbol || "整组")}</strong><span>{String(decision.reason || "-")}</span></li>)}</ul> : <p className="empty-text">暂无策略等待或阻断记录。</p>}</details>
              <details><summary>查看策略规则与版本</summary><pre>{JSON.stringify(group.strategy, null, 2)}</pre></details>
            </div>}
          </article>;
        })}
      </section>
    </div>
  );
}
