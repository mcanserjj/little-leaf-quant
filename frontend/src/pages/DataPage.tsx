import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { AIBadge } from "../components/AIBadge";
import type { Coverage, ProviderStatus } from "../types";

type SourceInfo = { label: string; provider: string; format: string; method: string; updateSupported: boolean; credential?: "hithink" | null };
type UpdateJob = { state?: "queued" | "running" | "completed" | "failed"; done?: number; total?: number | null; message?: string };
type UpdateState = { sources: Record<string, SourceInfo>; jobs: Record<string, UpdateJob>; newsAutoSync?: { enabled: boolean; intervalMinutes: number } };

function size(bytes: number) { return bytes < 102.4 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }

function CredentialCard({ name, description, status, path, placeholder, onChanged }: { name: string; description: string; status: ProviderStatus; path: string; placeholder: string; onChanged: () => void }) {
  const [key, setKey] = useState("");
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  async function act(action: "save" | "test" | "clear") {
    if (action === "clear" && !window.confirm(`确定清除${name} API Key？清除后相关功能会立即停止。`)) return;
    setWorking(true); setMessage("");
    try {
      if (action === "save") {
        const result = await api<ProviderStatus>(`${path}/key`, { method: "PUT", body: JSON.stringify({ apiKey: key }) });
        setKey(""); setMessage(result.storage === "dpapi" ? "已使用 Windows DPAPI 加密保存。" : "当前环境无法使用 DPAPI，仅保存在本次服务内存中。");
      } else if (action === "test") {
        await api(`${path}/test`, { method: "POST", body: key.trim() ? JSON.stringify({ apiKey: key }) : undefined });
        setMessage("连接测试通过。未记录或显示明文 Key。");
      } else {
        await api(`${path}/key`, { method: "DELETE" });
        setKey(""); setMessage("API Key 已从服务内存和本地加密文件中移除。");
      }
      onChanged();
    } catch (error) { setMessage(error instanceof Error ? error.message : "操作失败"); }
    finally { setWorking(false); }
  }
  return <article className="credential-card"><div className="panel-title"><h2>{name}</h2><AIBadge configured={status.configured} label={status.configured ? `已配置 ${status.masked || ""}` : "未配置"} /></div><p>{description}</p><label>API Key<input type="password" value={key} onChange={(event) => setKey(event.target.value)} placeholder={placeholder} autoComplete="off" /></label><div className="credential-actions"><button className="primary" disabled={working || !key.trim()} onClick={() => act("save")}>保存</button><button className="secondary" disabled={working || (!key.trim() && !status.configured)} onClick={() => act("test")}>测试连接</button><button className="danger" disabled={working || !status.configured} onClick={() => act("clear")}>安全清除</button></div>{message && <p className="action-message">{message}</p>}<div className="privacy-note">优先使用 Windows DPAPI；失败时只保存在服务进程内存。密钥不会写入浏览器存储、日志或普通配置文件。</div></article>;
}

export function DataPage({ coverage, deepseek, hithink, onChanged }: { coverage: Coverage[]; deepseek: ProviderStatus; hithink: ProviderStatus; onChanged: () => void }) {
  const [updates, setUpdates] = useState<UpdateState>({ sources: {}, jobs: {} });
  const [message, setMessage] = useState("");
  async function loadUpdates() { setUpdates(await api<UpdateState>("/api/data/updates")); }
  useEffect(() => { loadUpdates().catch(() => undefined); const timer = window.setInterval(() => loadUpdates().catch(() => undefined), 2000); return () => window.clearInterval(timer); }, []);
  const coverageMap = useMemo(() => Object.fromEntries(coverage.map((item) => [item.key, item])), [coverage]);
  async function update(key: string) {
    setMessage("");
    try { await api(`/api/data/updates/${key}`, { method: "POST" }); await loadUpdates(); setMessage(`${updates.sources[key]?.label || key}已交给后台更新。`); }
    catch (error) { setMessage(error instanceof Error ? error.message : "启动更新失败"); }
  }
  async function setNewsAutoSync(enabled: boolean) {
    try {
      await api("/api/data/news/auto-sync", { method: "PUT", body: JSON.stringify({ enabled }) });
      await loadUpdates();
      setMessage(enabled ? "场外资讯已启用每10分钟增量检查。" : "场外资讯自动同步已关闭，仍可手动更新。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "自动同步设置失败"); }
  }
  return <div className="page">
    <header className="page-header"><div><p className="eyebrow">真实数据 · 本机配置</p><h1>数据与设置</h1><p>查看每类数据的来源、格式、更新方式和实际进度，并管理服务端密钥。</p></div></header>
    {message && <p className="action-message">{message}</p>}
    <section className="source-panel"><div className="panel-title"><h2>数据来源与手动更新</h2><span>{coverage.filter((item) => item.available).length}/{coverage.length} 个本地覆盖项 · {Object.keys(updates.sources).length} 类数据说明</span></div><div className="source-list">{Object.entries(updates.sources).map(([key, source]) => {
      const item = coverageMap[key]; const job = updates.jobs[key] || {}; const total = job.total || 0; const percent = total ? Math.min(100, (Number(job.done || 0) / total) * 100) : 0;
      const credentialReady = source.credential === null || hithink.configured;
      return <article className="source-row" key={key}><span className={`status-dot ${item?.available ? "on" : ""}`} /><div><strong>{source.label}</strong><small>{source.provider} · {source.format}</small><p>{source.method}</p>{job.state && <div className={`job-state ${job.state}`}><span>{job.message || job.state}</span>{job.state === "running" && <div><i style={{ width: `${percent}%` }} /></div>}</div>}</div><div className="source-stats"><span>{item ? `${item.files} 文件 · ${size(item.bytes)}` : "尚无覆盖统计"}</span><small>{item?.completeCompanies != null && item.totalCompanies ? `${item.completeCompanies}/${item.totalCompanies} 家` : item?.updatedAt ? new Date(item.updatedAt).toLocaleString() : "无更新时间"}</small></div><div className="source-actions"><button className="secondary" disabled={!source.updateSupported || !credentialReady || job.state === "running" || job.state === "queued"} onClick={() => update(key)}>{job.state === "running" || job.state === "queued" ? "更新中" : "手动更新"}</button>{key === "user_data_research_news" && <label className="auto-sync"><input type="checkbox" checked={Boolean(updates.newsAutoSync?.enabled)} onChange={(event) => setNewsAutoSync(event.target.checked)} />10分钟自动</label>}</div></article>;
    })}</div></section>
    <section className="credential-grid"><CredentialCard name="DeepSeek" description="用于资讯研判、复盘解说和策略研究，不参与确定性选股与模拟成交。" status={deepseek} path="/api/ai" placeholder="sk-••••••••" onChanged={onChanged} /><CredentialCard name="HiThink Financial API" description="用于代码表、日K、财务、公司行动及龙虎榜等公开数据。当前版本没有内嵌你的 Key。" status={hithink} path="/api/hithink" placeholder="HiThink API Key" onChanged={onChanged} /></section>
  </div>;
}
