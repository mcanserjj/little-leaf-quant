import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { Shell, type PageKey } from "./components/Shell";
import { DataPage } from "./pages/DataPage";
import { LeaguePage } from "./pages/LeaguePage";
import { PortfolioPage } from "./pages/PortfolioPage";
import { SelectionPage } from "./pages/SelectionPage";
import type { Group, Overview } from "./types";

export default function App() {
  const [page, setPage] = useState<PageKey>("portfolio");
  const [groups, setGroups] = useState<Group[]>([]);
  const [overview, setOverview] = useState<Overview>();
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try { const [groupRows, overviewRow] = await Promise.all([api<Group[]>("/api/groups"), api<Overview>("/api/overview")]); setGroups(groupRows); setOverview(overviewRow); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取服务"); }
  }, []);
  useEffect(() => { load(); const timer = window.setInterval(load, 5000); return () => window.clearInterval(timer); }, [load]);
  return <Shell page={page} setPage={setPage} aiReady={Boolean(overview?.ai.configured)}>
    {error && <div className="service-error">服务连接失败：{error}</div>}
    {page === "portfolio" && <PortfolioPage groups={groups} overview={overview} refresh={load} />}
    {page === "selection" && <SelectionPage groups={groups} aiReady={Boolean(overview?.ai.configured)} refresh={load} />}
    {page === "league" && <LeaguePage groups={groups} aiReady={Boolean(overview?.ai.configured)} />}
    {page === "data" && <DataPage coverage={overview?.coverage || []} deepseek={overview?.ai || { configured: false }} hithink={overview?.hithink || { configured: false }} onChanged={load} />}
  </Shell>;
}
