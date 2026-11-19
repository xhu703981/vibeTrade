// Data pushed by the backend. All text is code-templated by the backend; symbols are sanitised (alphanumeric, <= 10 chars).
export type BackendEvent = {
  id: number;
  type: string;
  speak: string;
  panel: Record<string, any>;
  ts: number;
  loud: boolean;
  replay?: boolean;
};

export type Card = {
  id: string;
  code: string;
  mint: string;
  symbol_display: string;
  amount_usdc: number;
  expected_out: number;
  metrics: Record<string, number>;
  sim: { token_out: number | null; usdc_in: number | null; sol_change_lamports: number | null; price_impact_pct: number };
  guard: { decision: "allow" | "ask_human" | "block"; reasons: string[]; jev_risk: number | null };
  created_at: number;
  expires_at: number;
  status: string;
  seconds_left: number;
  coin_number?: number | null;
  kind?: string;
  ticker?: string | null;
  sell_fraction?: number | null;
  adjust?: Record<string, unknown> | null;
  stop_loss_pct?: number | null;
  stop_basis?: string | null;
  atr_pct?: number | null;
  risk_usdc?: number | null;
  loss_plan_usdc?: number | null;
  loss_worst_usdc?: number | null;
  cap_note?: string | null;
  derisk?: boolean;
  offer?: string | null;
  suspicion?: { label: string; score: number } | null;
  warning?: string | null;
};

export type Position = {
  mint: string;
  mark_usdc?: number | null;   // backend: what the held tokens sell for now, fees included (same mark as the spoken offers)
  pnl_usdc?: number | null;    // backend: realized + mark - cost
  symbol_display: string;
  initial_tokens: number;
  held_tokens: number;
  entry_usdc: number;
  realized_usdc: number;
  peak_ratio: number;
  last_ratio: number | null;          // price move since the fill (exit-rule ratio)
  last_value_usdc?: number | null;    // current sell value of the held tokens
  status: string;
  feed_down: boolean;
  price_at?: number | null;
  coin_number?: number | null;
  ticker?: string | null;
  stop_loss_pct?: number | null;
  sells: { trigger: string; amount: number; usdc: number; sig: string; ratio: number }[];
};

export type SpendEntry = { seller: string; amount_usdc: number; status: string; coin_mint: string; reason: string; tx_hash: string | null; ts: number };

// Selection desk: every coin that passed this round, numbered (coin 1..N). Metrics are backend numbers only.
export type DeskMetrics = Partial<Record<
  | "liquidity_usd" | "market_cap" | "volume_h24" | "volume_h1" | "volume_h6" | "price_usd" | "age_hours" | "volume_to_liquidity"
  | "price_change_h1" | "price_change_h6" | "price_change_h24" | "buys_h1" | "sells_h1" | "buys_h24" | "sells_h24"
  | "holders" | "top10_pct" | "dev_pct" | "roundtrip_loss_pct" | "price_impact_pct" | "lp_burned_pct" | "lp_locked_pct"
  | "pair_created_at_ms", number | null>> & { mint_revoked?: boolean | null; freeze_revoked?: boolean | null; known_rugger?: boolean | null };

export type DeskOffer = {
  id: string;
  number: number;
  round: number;
  mint: string;
  symbol: string;
  pool: string | null;
  dex: string | null;
  paid_promo: boolean;
  metrics: DeskMetrics;
  status: "open" | "building" | "carded" | "executing" | "bought" | "skipped" | string;
  card: { id: string; code: string; status: string; amount_usdc: number } | null;
  market_live: boolean;
  market_at: number | null;
  signals: { good: string[]; caution: string[] };
  kind?: "meme" | "major" | string;
  ticker?: string | null;
  reason?: string | null;
  receipt?: { seller: string; amount_usdc: number; cached: boolean; tx_hash?: string | null; recorded_usdc?: number | null }[];
  suspicion?: { key?: string; label: string; score: number } | null;
  stop?: { stop_pct: number; atr_pct: number | null; basis: string } | null;
  /** a concrete-number worry ("Top 10 wallets hold 28%"), only when it is worth asking about */
  warning?: string | null;
  /** the rehearsal-only red-team test coin (VIBE_REDTEAM=1); never listed with the real picks */
  redteam?: boolean;
};

export type Desk = { offers: DeskOffer[]; majors?: DeskOffer[]; cap_usdc: number; per_trade_usdc: number | null; remaining_usdc: number; round: number };

export type Candles = { ok: boolean; reason?: string; candles: [number, number, number, number, number, number][]; source?: string; tf?: string; stale?: boolean; fetched_at?: number; price?: number };

export type Recap = { ready: boolean; missing_required: string[]; text: string | null; digest: string | null };

export type AppState = {
  mandate_session: {
    frozen: boolean; draft?: Record<string, any>; missing_required?: string[]; stage?: string; recap?: Recap | null; mandate?: any;
    amending?: boolean; amend_kind?: "none" | "filters" | "guards"; amend_changes?: string[]; active?: any;
  };
  phase: string;
  modes: { discovery: string; research: string; ui_buttons: boolean };
  mandate: { mandate_id: string; hash: string; expires_at: string } | null;
  candidates: any[];
  research: any[];
  spend: { budget_usdc: number; spent_usdc: number; entries: SpendEntry[]; topups: any[] } | null;
  topup: { needed_usdc: number; spent_usdc: number; budget_usdc: number; suggest_usdc: number; expires_at: number } | null;
  engine: { wallet: string; spent_usdc: number; cards: Card[]; positions: Position[]; trades: any[] } | null;
  voice_configured: boolean;
  desk?: Desk;
  amend?: { allowed: boolean; blocked_reason: string | null };
  round?: number;
  mandate_versions?: number;
  price_test_enabled?: boolean;
  /** duplex: background work still running (research round, card building, swaps, the exit) */
  working_on?: string[];
  round_running?: boolean;
  /** shadow listener: the staged reading of her latest words (display only) */
  hearing?: { kind: string; label: string; final: boolean; at: number } | null;
};
