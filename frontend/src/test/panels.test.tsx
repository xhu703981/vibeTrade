import { guardSentence } from "../ui/sheets";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CardView } from "../panels/CardsPanel";
import { FilterTable, editUtterance, FILTER_ROWS } from "../panels/FilterTable";
import { GuardAlert, latestAlert } from "../panels/GuardAlert";
import { PositionsPanel, pnl } from "../panels/PositionsPanel";
import { TopupPanel } from "../panels/TopupPanel";
import { SelectionDesk, age, failedCoins, validateOrders } from "../panels/SelectionDesk";
import { PriceChart, fmtPrice } from "../panels/PriceChart";
import type { Api } from "../api";
import type { BackendEvent, Card, Desk, DeskOffer, Position } from "../types";

// lightweight-charts needs a real canvas; in jsdom record what we draw instead
const drawn: { candles: any[]; removed: number; lines: any[] } = { candles: [], removed: 0, lines: [] };
vi.mock("lightweight-charts", () => {
  const series = (kind: string) => ({ setData: (d: any[]) => { if (kind === "candle") drawn.candles = d; }, priceScale: () => ({ applyOptions: () => {} }),
    update: () => {}, createPriceLine: (o: any) => { drawn.lines.push(o); return o; }, removePriceLine: () => {} });
  return {
    CandlestickSeries: "candle", HistogramSeries: "hist", ColorType: { Solid: "solid" }, LineStyle: { Solid: 0, Dashed: 2 },
    createChart: () => ({
      addSeries: (k: string) => series(k), priceScale: () => ({ applyOptions: () => {} }), timeScale: () => ({ fitContent: () => {} }),
      remove: () => { drawn.removed++; },
    }),
  };
});

const card = (o: Partial<Card> = {}): Card => ({
  id: "c1", code: "47", mint: "M", symbol_display: "GATO", amount_usdc: 10, expected_out: 1, metrics: { liquidity_usd: 120000, top10_pct: 15 },
  sim: { token_out: 1006784634, usdc_in: 10000000, sol_change_lamports: -2039280, price_impact_pct: 0.09 },
  guard: { decision: "allow", reasons: [], jev_risk: 0.06 }, created_at: 1000, expires_at: 1180, status: "pending", seconds_left: 180, ...o,
});

describe("confirmation card", () => {
  it("shows code and countdown; approve disabled when the button channel is off, reject always available", () => {
    const onApprove = vi.fn(), onReject = vi.fn();
    render(<CardView card={card()} now={1100} buttonsEnabled={false} onApprove={onApprove} onReject={onReject} />);
    expect(screen.getByText("47")).toBeInTheDocument();
    // the countdown is a draining line, no ticking numbers
    expect(screen.getByRole("timer")).toHaveTextContent("");
    expect((screen.getByRole("timer").firstChild as HTMLElement).style.width).toMatch(/%$/);
    expect(screen.getByText("Passed")).toHaveClass("up");
    expect(screen.getByRole("button", { name: "Approve 47" })).toBeDisabled();
    expect(screen.getByText('Say "approve 47" or "reject 47"')).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onReject).toHaveBeenCalled();
    expect(onApprove).not.toHaveBeenCalled();
  });
  it("can approve when the button channel is on; buttons disappear after timeout", () => {
    const onApprove = vi.fn();
    const { rerender } = render(<CardView card={card()} now={1100} buttonsEnabled onApprove={onApprove} onReject={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve 47" }));
    expect(onApprove).toHaveBeenCalledWith(expect.objectContaining({ code: "47" }));
    rerender(<CardView card={card()} now={1200} buttonsEnabled onApprove={onApprove} onReject={() => {}} />);
    expect(screen.queryByRole("button", { name: "Approve 47" })).toBeNull();
  });
  it("renders the token symbol as text, not HTML", () => {
    render(<CardView card={card({ symbol_display: "<b>x</b>" })} now={1100} buttonsEnabled onApprove={() => {}} onReject={() => {}} />);
    expect(screen.getByText("<b>x</b>")).toBeInTheDocument();
  });
});

describe("guard red warning", () => {
  const evs: BackendEvent[] = [
    { id: 1, type: "card_created", speak: "x", panel: {}, ts: 0, loud: false },
    { id: 2, type: "guard_blocked", speak: "The guard blocked the buy transaction, so no card was created.", panel: { reasons: ["transfer to an unknown address"] }, ts: 0, loud: false },
  ];
  it("shows the latest one and hides it after dismissal", () => {
    const onDismiss = vi.fn();
    const a = latestAlert(evs, 0);
    render(<GuardAlert event={a} onDismiss={onDismiss} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Blocked");
    expect(screen.getByRole("alert")).toHaveTextContent("transfer to an unknown address");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledWith(2);
    expect(latestAlert(evs, 2)).toBeNull();
    expect(latestAlert([...evs, { id: 3, type: "mandate_frozen", speak: "", panel: {}, ts: 0, loud: false }], 0)).toBeNull();
  });
});

describe("settings table", () => {
  it("an edit becomes a user sentence checked by the backend", async () => {
    const onEdit = vi.fn();
    render(<FilterTable draft={{ filters: { min_liquidity_usd: 50000 } }} frozen={false} missing={["budget.total_usdc"]} onEdit={onEdit} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit min liquidity" }));
    const input = screen.getByLabelText("New min liquidity");
    fireEvent.change(input, { target: { value: "80000" } });
    await act(async () => { fireEvent.submit(input.closest("form")!); });
    expect(onEdit).toHaveBeenCalledWith("Set min liquidity to 80000 dollars");
    expect(editUtterance(FILTER_ROWS[4], "25")).toBe("Set stop loss to 25%");
    expect(editUtterance(FILTER_ROWS[0], "50 USDC")).toBe("Set total budget to 50 USDC");
  });
  it("labels the defaults as a proposal during the filter review", () => {
    render(<FilterTable draft={{}} frozen={false} missing={[]} stage="filters" onEdit={() => {}} />);
    expect(screen.getByText(/These default coin filters are my proposal/)).toBeInTheDocument();
  });
  it("running mandate: not editable only while a change is blocked (no amend permission given)", () => {
    render(<FilterTable draft={{}} frozen missing={[]} onEdit={() => {}} />);
    expect(screen.queryByRole("button", { name: /^Edit/ })).toBeNull();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.queryByText(/read-only/i)).toBeNull();
  });
});

describe("positions / PnL", () => {
  const pos: Position = { mint: "M", symbol_display: "GATO", initial_tokens: 1000, held_tokens: 500, entry_usdc: 10e6, realized_usdc: 11.18e6,
    peak_ratio: 2.2, last_ratio: 2.0, status: "open", feed_down: false, sells: [{ trigger: "take_profit", amount: 500, usdc: 11.18e6, sig: "3nX6J83E3zAAwnQaKf1f", ratio: 2.24 }] };
  it("PnL = sold back + remaining value - cost", () => {
    expect(pnl(pos).value).toBeCloseTo(11.18e6 + 10e6 - 10e6);
    render(<PositionsPanel positions={[pos]} canExit onEmergency={() => {}} />);
    expect(screen.getByText(/Take-profit: sold at 2.24\u00d7/)).toBeInTheDocument();
    expect(screen.getByText(/P&L \+\$11.18/)).toBeInTheDocument();
  });
});

describe("positions: cent-sized trade, price move since the fill", () => {
  it("value comes from the live sell quote; cents keep 4 decimals; small moves 3 decimals", () => {
    const p: Position = { mint: "M", symbol_display: "GATO", initial_tokens: 1000, held_tokens: 1000, entry_usdc: 10_000, realized_usdc: 0,
      peak_ratio: 1.004, last_ratio: 1.004, last_value_usdc: 9_883, status: "open", feed_down: false, sells: [] };
    expect(pnl(p).markUsdc).toBe(9_883);
    render(<PositionsPanel positions={[p]} canExit onEmergency={() => {}} />);
    expect(screen.getByText("Price 1.004\u00d7")).toBeInTheDocument();
    expect(screen.getByText("Value $0.0099")).toBeInTheDocument();
    expect(screen.getByText("P&L \u2212$0.0001")).toBeInTheDocument();
  });
});

describe("research top-up", () => {
  it("shows amounts and can be declined", () => {
    const onReject = vi.fn();
    render(<TopupPanel topup={{ needed_usdc: 0.05, spent_usdc: 1, budget_usdc: 1, suggest_usdc: 0.5, expires_at: Date.now() / 1000 + 60 }}
                       buttonsEnabled onApprove={() => {}} onReject={onReject} />);
    expect(screen.getByRole("alertdialog")).toHaveTextContent("the next step needs $0.05");
    fireEvent.click(screen.getByRole("button", { name: "Don't add" }));
    expect(onReject).toHaveBeenCalled();
  });
});

describe("rehearsal price tests and buy amount", () => {
  const pos: Position = { mint: "M", symbol_display: "GATO", initial_tokens: 1000, held_tokens: 1000, entry_usdc: 10e6, realized_usdc: 0,
    peak_ratio: 1, last_ratio: 1, status: "open", feed_down: false, sells: [] };
  it("test buttons only when enabled (rehearsal), and they call onTest", () => {
    const { rerender } = render(<PositionsPanel positions={[pos]} canExit onEmergency={() => {}} />);
    expect(screen.queryByRole("button", { name: "Test take profit" })).toBeNull();
    const onTest = vi.fn();
    rerender(<PositionsPanel positions={[pos]} canExit onEmergency={() => {}} testEnabled onTest={onTest} />);
    fireEvent.click(screen.getByRole("button", { name: "Test take profit" }));
    fireEvent.click(screen.getByRole("button", { name: "Test stop loss" }));
    expect(onTest.mock.calls).toEqual([["take_profit"], ["stop_loss"]]);
  });
});

const offer = (n: number, o: Partial<DeskOffer> = {}): DeskOffer => ({
  id: `r1c${n}`, number: n, round: 1, mint: `MINT${n}`, symbol: `SYM${n}`, pool: `POOL${n}`, dex: "pumpswap", paid_promo: n === 2,
  metrics: { market_cap: 900000, liquidity_usd: 120000, volume_h24: 240000, volume_to_liquidity: n === 2 ? 15 : 2, age_hours: 72, holders: 4000,
             top10_pct: 15, dev_pct: 0.5, roundtrip_loss_pct: 1.2, price_usd: 0.00001234, price_change_h1: -2.5, price_change_h24: 12,
             buys_h1: 40, sells_h1: 30, lp_burned_pct: 100, mint_revoked: true, freeze_revoked: true },
  status: "open", card: null, market_live: true, market_at: 0, signals: { good: ["LP 100% burned"], caution: n === 2 ? ["paid promotion on DexScreener (boosted listing)"] : [] },
  ...o,
});
const deskOf = (offers: DeskOffer[], o: Partial<Desk> = {}): Desk => ({ offers, cap_usdc: 10, per_trade_usdc: 10, remaining_usdc: 12, round: 2, ...o });
const chartApi = (resp: any = { ok: true, source: "GeckoTerminal", candles: [[1, 1, 2, 0.5, 1.5, 10], [2, 1.5, 3, 1, 2.5, 20]] }) => {
  const gets: string[] = [];
  const api: Api = { get: async (p) => { gets.push(p); if (resp instanceof Error) throw resp; return resp; }, post: async () => ({ ok: true }) };
  return { api, gets };
};

describe("selection desk", () => {
  it("compares every passed coin as a tile with trader numbers and flags", async () => {
    const { api } = chartApi();
    render(<SelectionDesk api={api} desk={deskOf([offer(1), offer(2), offer(3)])} failed={[]} buttonsEnabled washMax={20}
                          onOrder={() => {}} onSkip={() => {}} onRefresh={() => {}} />);
    for (const n of [1, 2, 3]) expect(screen.getByRole("article", { name: `Coin ${n}` })).toBeInTheDocument();
    const t2 = screen.getByRole("article", { name: "Coin 2" });
    expect(t2).toHaveTextContent("Paid promo");
    expect(t2).toHaveTextContent("15.0\u00d7, possible wash trading");
    expect(t2).toHaveTextContent("$900.0k");
    expect(t2).toHaveTextContent("LP 100% burned");
    expect(t2).toHaveTextContent("Mint revoked");
    expect(t2).toHaveTextContent("\u22122.5%");
    expect(t2).toHaveTextContent("40 / 30");
    expect(screen.getByRole("article", { name: "Coin 1" })).not.toHaveTextContent("wash trading");
    expect(await screen.findByTestId("kline")).toBeInTheDocument();
    await waitFor(() => expect(drawn.candles.map((c) => c.close)).toEqual([1.5, 2.5]));
  });
  it("multi-select: one amount each, cap and remaining budget checked, one order per coin", async () => {
    const { api } = chartApi();
    const onOrder = vi.fn(), onSkip = vi.fn();
    render(<SelectionDesk api={api} desk={deskOf([offer(1), offer(2), offer(3)])} failed={[]} buttonsEnabled onOrder={onOrder} onSkip={onSkip} onRefresh={() => {}} />);
    fireEvent.click(screen.getByLabelText("Pick coin 1"));
    fireEvent.click(screen.getByLabelText("Pick coin 3"));
    const create = () => screen.getByRole("button", { name: /^Create/ });
    expect(create()).toBeDisabled();                                       // no amount yet
    fireEvent.change(screen.getByLabelText("Same amount for all picked"), { target: { value: "7" } });
    expect(screen.getByRole("status")).toHaveTextContent("Total 14 dollars is more than the 12 dollars left");
    fireEvent.change(screen.getByLabelText("Amount for coin 3"), { target: { value: "11" } });
    expect(screen.getByRole("status")).toHaveTextContent("above the per-trade cap of 10");
    fireEvent.change(screen.getByLabelText("Amount for coin 3"), { target: { value: "5" } });
    expect(create()).toHaveTextContent("Create 2 cards");
    await act(async () => { fireEvent.click(create()); });
    expect(onOrder).toHaveBeenCalledWith([{ coin_number: 1, amount_usdc: 7 }, { coin_number: 3, amount_usdc: 5 }]);
    expect(screen.getByText(/Or say: “coin/)).toBeInTheDocument();
  });
  it("carded / bought coins cannot be picked; order button off without the button channel; failed coins collapsed", async () => {
    const { api } = chartApi({ ok: false, reason: "rate limited", candles: [] });
    render(<SelectionDesk api={api} desk={deskOf([offer(1, { status: "carded", card: { id: "c", code: "47", status: "pending", amount_usdc: 5 } }), offer(2)])}
                          failed={[{ mint: "BAD1", symbol: "RUG", reasons: ["top 10 hold 70%"] }]} buttonsEnabled={false}
                          onOrder={() => {}} onSkip={() => {}} onRefresh={() => {}} />);
    expect(screen.getByLabelText("Pick coin 1")).toBeDisabled();
    expect(screen.getByRole("article", { name: "Coin 1" })).toHaveTextContent("Card waiting, card 47");
    fireEvent.click(screen.getByLabelText("Pick coin 2"));
    fireEvent.change(screen.getByLabelText("Amount for coin 2"), { target: { value: "3" } });
    expect(screen.getByRole("button", { name: /^Create/ })).toBeDisabled();
    expect(screen.queryByText("RUG")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Show 1 coin that did not pass/ }));
    expect(screen.getByText("RUG")).toBeInTheDocument();
    expect(await screen.findByText(/Candles aren't available right now \(rate limited\)/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show the DexScreener chart instead" }));
    expect(screen.getByTitle("DexScreener chart")).toHaveAttribute("src", expect.stringContaining("dexscreener.com/solana/POOL1"));
  });
  it("empty round explains how to go on", () => {
    const { api } = chartApi();
    render(<SelectionDesk api={api} desk={deskOf([])} failed={[]} buttonsEnabled onOrder={() => {}} onSkip={() => {}} onRefresh={() => {}} />);
    expect(screen.getByText(/No meme coin passed this round/)).toHaveTextContent("run another round");
    expect(screen.queryByRole("button", { name: /^Create/ })).toBeNull();
  });
  it("chart switches timeframe through the backend; helpers", async () => {
    const { api, gets } = chartApi();
    render(<PriceChart api={api} pool="POOLX" label="Coin 1" />);
    await screen.findByTestId("kline");
    fireEvent.click(screen.getByRole("tab", { name: "1h" }));
    await act(async () => {});
    expect(gets).toEqual(["/market/ohlcv/POOLX?tf=15m", "/market/ohlcv/POOLX?tf=1h"]);
    expect(fmtPrice(0.00001234)).toBe("0.00001234");
    expect(fmtPrice(1.5)).toBe("1.5000");
    expect(age(0.5)).toBe("30m");
    expect(age(72)).toBe("3.0d");
    expect(validateOrders([1], {}, "", 10, 50).error).toContain("Give coin 1 an amount");
    expect(failedCoins([{ mint: "a", status: "fail_free", reasons: ["low liq"] }, { mint: "b", status: "pending_research" }],
                       [{ mint: "b", status: "pass" }])).toEqual([{ mint: "a", symbol: "", reasons: ["low liq"] }]);
  });
});

describe("settings table in a running session", () => {
  it("frozen + amend allowed: rows editable, another round button; while changing: cancel / confirm", () => {
    const onEdit = vi.fn(), onApply = vi.fn(), onCancel = vi.fn();
    const draft = { budget: { total_usdc: 50, per_trade_usdc: 10 }, filters: { market_cap_usd: [100000, 400000] } };
    const { rerender } = render(<FilterTable draft={draft} frozen missing={[]} amendAllowed version={1} onEdit={onEdit} onApplyAmend={onApply} onCancelAmend={onCancel} />);
    expect(screen.getByText("Active, version 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit market cap range" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run another research round" }));
    expect(onApply).toHaveBeenCalledWith("none");
    rerender(<FilterTable draft={draft} frozen missing={[]} amendAllowed={false} blockedReason="a trade is executing" onEdit={onEdit} />);
    expect(screen.queryByRole("button", { name: "Edit market cap range" })).toBeNull();
    expect(screen.getByText(/Changes are paused right now: a trade is executing/)).toBeInTheDocument();
    rerender(<FilterTable draft={draft} frozen={false} missing={[]} amending amendKind="guards" amendChanges={["budget.per_trade_usdc"]} version={1}
                          onEdit={onEdit} onApplyAmend={onApply} onCancelAmend={onCancel} />);
    expect(screen.getByText("Changing, version 2")).toBeInTheDocument();
    expect(screen.getByText("Per-trade cap").closest("tr")).toHaveClass("changed");
    fireEvent.click(screen.getByRole("button", { name: "Confirm and start research" }));
    expect(onApply).toHaveBeenLastCalledWith("guards");
    fireEvent.click(screen.getByRole("button", { name: "Cancel changes" }));
    expect(onCancel).toHaveBeenCalled();
    rerender(<FilterTable draft={draft} frozen={false} missing={[]} amending amendKind="filters" onEdit={onEdit} onApplyAmend={onApply} />);
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));
    expect(onApply).toHaveBeenLastCalledWith("filters");
  });
  it("card shows its coin number", () => {
    render(<CardView card={card({ coin_number: 3 })} now={1100} buttonsEnabled onApprove={() => {}} onReject={() => {}} />);
    expect(screen.getByText("Coin 3")).toBeInTheDocument();
  });
});

describe("guard block flow", () => {
  it("shows the red USDC -> unknown wallet line and expected vs simulated flow", () => {
    const ev = { id: 9, type: "guard_blocked", ts: 0, loud: true,
      speak: "The buy for coin 4 was blocked by the guard: it would also move USDC to a wallet that isn't yours. Nothing was sent.",
      panel: { reasons: ["instruction 6: forbidden Token instruction Transfer"], flow: {
        alerts: ["USDC \u2192 unknown wallet: \u22120.1 USDC"], simulation_ok: true,
        expected: [{ what: "USDC out of your wallet, to the pool", usdc: -0.1 }, { what: "coin in", tokens: 5000 }],
        simulated: [{ what: "USDC out of your wallet", usdc: -0.2 }, { what: "coin in", tokens: 5000 },
                    { what: "USDC to an unknown wallet (not yours)", usdc: -0.1, alert: true }] } } } as unknown as BackendEvent;
    render(<GuardAlert event={ev} onDismiss={() => {}} />);
    expect(screen.getByText("USDC to unknown wallet: \u22120.1 USDC")).toBeTruthy();
    expect(guardSentence(ev)).toBe("this trade would also send USDC to a wallet that isn't yours. Nothing was sent.");
    expect(screen.getByText("Expected")).toBeTruthy();
    expect(screen.getByText("Simulated")).toBeTruthy();
    expect(screen.getByText("USDC to an unknown wallet (not yours)").parentElement?.className).toContain("bad");
  });
});

describe("K-line position lines", () => {
  it("draws the last price as a label plus dashed entry and stop lines when she holds the coin", async () => {
    drawn.lines = [];
    const api = { get: vi.fn(async () => ({ ok: true, candles: [[1, 1, 1, 1, 1, 10], [2, 1, 2, 1, 2, 10]] })) } as any;
    render(<PriceChart api={api} pool="P" compact lines={{ entryRatio: 2, stopPct: 25 }} />);
    await screen.findByTestId("kline");
    await waitFor(() => expect(drawn.lines.length).toBe(3));
    const [last, entry, stop] = drawn.lines;
    expect(last.price).toBe(2);
    expect(last.lineVisible).toBe(false);
    expect(entry).toMatchObject({ price: 1, title: "Entry", lineStyle: 2 });
    expect(stop).toMatchObject({ price: 0.75, title: "Stop", lineStyle: 2 });
    expect(screen.getByRole("button", { name: "DexScreener" })).toBeInTheDocument();
  });
});

describe("Today uses the backend's mark", () => {
  it("prefers mark_usdc (fees included) over the raw price ratio", () => {
    const p: Position = { mint: "M", symbol_display: "X", initial_tokens: 1000, held_tokens: 1000, entry_usdc: 1_000_000, realized_usdc: 0,
      peak_ratio: 1.12, last_ratio: 1.12, mark_usdc: 969_920, pnl_usdc: -30_080, status: "open", feed_down: false, sells: [] };
    expect(pnl(p).value).toBe(-30_080);
    expect(pnl(p).markUsdc).toBe(969_920);
  });
});
