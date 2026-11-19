import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AppState, DeskOffer } from "../types";
import { sparkPath } from "../ui/Sparkline";
import { PicksSheet, dataSources, listedPicks, picksSource, signedPct } from "../ui/sheets";

const offer = (n: number, extra: Partial<DeskOffer> = {}): DeskOffer => ({
  id: `o${n}`, number: n, round: 1, mint: `M${n}`, symbol: `SYM${n}`, pool: null, dex: null, paid_promo: false,
  metrics: { price_usd: 0.0032, price_change_h24: n === 2 ? -13.24 : 155 } as any, status: "open", card: null,
  market_live: false, market_at: null, signals: { good: [], caution: [] }, reason: `${n * 5} smart wallets bought $5k today`, ...extra,
});
const api = { get: vi.fn(() => new Promise(() => {})) } as any;
const state = (offers: DeskOffer[], spent = 0) => ({
  desk: { offers, cap_usdc: 1, per_trade_usdc: 1, remaining_usdc: 5, round: 1 },
  candidates: new Array(25).fill({}), research: [{ status: "pass" }, { status: "pass" }, { status: "pass" }, { status: "fail" }],
  spend: { budget_usdc: 1, spent_usdc: spent, entries: [], topups: [] },
}) as unknown as AppState;

describe("Today's picks", () => {
  it("signs with a true minus and one decimal", () => {
    expect(signedPct(4.24)).toBe("+4.2%");
    expect(signedPct(-13.24)).toBe("\u221213.2%");
    expect(signedPct(155.04)).toBe("+155.0%");
  });

  it("says how many passed, and the cost only when money was spent", () => {
    expect(picksSource(state([offer(1)]))).toBe("3 of 25 coins passed.");
    expect(picksSource(state([offer(1)], 0.24))).toBe("3 of 25 coins passed. Research cost $0.24.");
  });

  it("keeps the red-team coin off the list and shows one row per pick", () => {
    const offers = [offer(1, { warning: "Top 10 wallets hold 28%" }), offer(2), offer(4, { symbol: "TESTCOIN", redteam: true })];
    expect(listedPicks(offers).map((o) => o.number)).toEqual([1, 2]);
    const open = vi.fn();
    render(<PicksSheet api={api} state={state(offers)} onOpenCoin={open} />);
    expect(screen.queryByText("TESTCOIN")).toBeNull();
    expect(screen.getByText("\u221213.2%")).toBeTruthy();
    expect(screen.getByText("Top 10 wallets hold 28%")).toBeTruthy();
    expect(screen.queryByText(/Here's what made the cut/)).toBeNull();
    const rows = screen.getAllByRole("button");
    expect(rows).toHaveLength(2);
    fireEvent.click(rows[1]);
    expect(open).toHaveBeenCalledWith(2);
  });

  it("puts the data source in the details, not the reason", () => {
    const o = offer(1, { metrics: { sm_traders: 5 } as any, receipt: [{ seller: "Quick Intel security scan", amount_usdc: 0, cached: true }] });
    expect(dataSources(o)).toBe("Data from Nansen smart money and Quick Intel security scan.");
    expect(o.reason).not.toMatch(/Nansen/);
  });

  it("marks only the last point of the sparkline", () => {
    const sp = sparkPath([1, 2, 3], 80, 24);
    expect(sp.className).toBe("up");
    expect(sp.last[0]).toBeCloseTo(77.5);
  });
});
