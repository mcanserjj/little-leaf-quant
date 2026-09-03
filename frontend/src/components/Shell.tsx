import type { ReactNode } from "react";

export type PageKey = "portfolio" | "selection" | "league" | "data";

const navigation: Array<{ key: PageKey; label: string; helper: string; icon: string }> = [
  { key: "portfolio", label: "五仓模拟", helper: "查看持仓与收益", icon: "▦" },
  { key: "selection", label: "选股研究", helper: "生成八组候选", icon: "⌕" },
  { key: "league", label: "联赛与复盘", helper: "对比、解释、进化", icon: "⌁" },
  { key: "data", label: "数据与设置", helper: "同步与服务配置", icon: "⚙" },
];

export function Shell({ page, setPage, children, aiReady }: {
  page: PageKey;
  setPage: (page: PageKey) => void;
  children: ReactNode;
  aiReady: boolean;
}) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => setPage("portfolio")}>
          <img src="/leaf-logo.jpg" alt="小树叶" />
          <span><strong>小树叶</strong><small>炒股模拟器</small></span>
        </button>
        <nav>
          {navigation.map((item) => (
            <button key={item.key} className={page === item.key ? "active" : ""} onClick={() => setPage(item.key)}>
              <span className="nav-icon">{item.icon}</span>
              <span><strong>{item.label}</strong><small>{item.helper}</small></span>
            </button>
          ))}
        </nav>
        <div className="sidebar-status">
          <span className={`status-dot ${aiReady ? "on" : ""}`} />
          <span><strong>DeepSeek</strong><small>{aiReady ? "AI 功能可用" : "尚未配置"}</small></span>
        </div>
        <p className="research-only">仅用于研究与模拟，不连接券商账户</p>
      </aside>
      <main>{children}</main>
    </div>
  );
}

