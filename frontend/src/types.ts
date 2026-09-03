export type Position = {
  symbol: string;
  name: string;
  quantity: number;
  buy_price?: number;
  average_cost?: number;
  buy_time?: string;
  highest_price?: number;
  current_price?: number;
  quote_time?: string;
  market_value?: number;
  unrealized_pnl?: number;
  return_pct?: number;
  strategy_version?: number;
};

export type Candidate = {
  symbol: string;
  name: string;
  close: number;
  score: number;
  roe?: number | null;
  pe_ttm?: number | null;
  valuation_date?: string | null;
  valuation_source?: string | null;
  dividend_ttm?: number | null;
  dividend_events_ttm?: number | null;
  dividend_yield?: number | null;
  entry_price_min?: number;
  entry_price_max?: number;
  selection_reason?: string[];
};

export type Group = {
  id: string;
  name: string;
  horizon: "short" | "long";
  status: string;
  returnPct: number;
  account: Record<string, number | string>;
  positions: Position[];
  trades: Array<Record<string, unknown>>;
  orders: Array<Record<string, unknown>>;
  decisions: Array<Record<string, unknown>>;
  candidates: { generated_at?: string; status?: string; notes?: string[]; items?: Candidate[] };
  strategy: Record<string, unknown>;
};

export type Coverage = {
  key: string;
  label: string;
  files: number;
  bytes: number;
  updatedAt?: string;
  available: boolean;
  completeCompanies?: number | null;
  totalCompanies?: number | null;
};

export type Overview = {
  updatedAt: string;
  lastQuoteAt?: string;
  quoteCount: number;
  groups: number;
  positions: number;
  averageReturnPct: number;
  coverage: Coverage[];
  ai: ProviderStatus;
  hithink: ProviderStatus;
  execution: ExecutionStatus;
};

export type ProviderStatus = { configured: boolean; masked?: string | null; storage?: "dpapi" | "memory" | null };

export type ExecutionStatus = {
  enabled: boolean;
  providerConfigured: boolean;
  state?: "waiting" | "blocked" | "paused" | "idle" | "running" | "error";
  reason?: string;
  checkedAt?: string;
  lastQuoteAt?: string;
  lastEvaluationAt?: string;
  quoteCount?: number;
  executionMode: "current_snapshot" | "next_snapshot";
  quoteRefreshSeconds: number;
  strategyEvaluationSeconds: number;
  calendarYears: number[];
};
