export function AIBadge({ configured, label = "AI" }: { configured: boolean; label?: string }) {
  return (
    <span className={`ai-badge ${configured ? "ready" : "needs-key"}`}>
      <span className="spark">✦</span> {label} · {configured ? "已连接" : "需配置"}
    </span>
  );
}

