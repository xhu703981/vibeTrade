import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LiveLog } from "../panels/LiveLog";
import { PositionsPanel, TxRef } from "../panels/PositionsPanel";
import { NetworkBadge } from "../ui/NetworkBadge";
import { OrderSheet, PositionSheet } from "../ui/sheets";
import type { AppState, Card, Position } from "../types";

const SIG = "5igSignature1111111111111111111111111111111111111111111111111111";
const URL = `https://solscan.io/tx/${SIG}`;

const pos = (o: Partial<Position> = {}): Position => ({
  mint: "M", symbol_display: "GATO", initial_tokens: 1000, held_tokens: 1000, entry_usdc: 1_000_000, realized_usdc: 0,
  peak_ratio: 1, last_ratio: 1, status: "open", feed_down: false, sells: [], ...o,
});

const card = (o: Partial<Card> = {}): Card => ({
  id: "c1", code: "47", mint: "M", symbol_display: "GATO", amount_usdc: 1, expected_out: 1, metrics: {},
  sim: { token_out: 1, usdc_in: 1_000_000, sol_change_lamports: 0, price_impact_pct: 0 },
  guard: { decision: "allow", reasons: [], jev_risk: 0 }, created_at: 1000, expires_at: 99999999999, status: "pending", seconds_left: 180, ...o,
});

describe("mainnet real funds", () => {
  it("shows the amber badge only when the backend says real funds", () => {
    const { rerender } = render(<NetworkBadge network={{ network: "fork", real_funds: false }} />);
    expect(screen.queryByTestId("mainnet-badge")).toBeNull();
    rerender(<NetworkBadge network={{ network: "mainnet", real_funds: true, max_trade_usd: 5 }} />);
    const b = screen.getByTestId("mainnet-badge");
    expect(b).toHaveTextContent(/Mainnet.*Real funds/);
    expect(b.getAttribute("title")).toContain("$5 per trade");
  });

  it("links a signature to Solscan, and only to Solscan", () => {
    const { rerender } = render(<TxRef sig={SIG} url={URL} />);
    const a = screen.getByRole("link");
    expect(a).toHaveAttribute("href", URL);
    expect(a).toHaveAttribute("target", "_blank");
    rerender(<TxRef sig={SIG} url="https://evil.example/tx/x" />);
    expect(screen.queryByRole("link")).toBeNull();
    rerender(<TxRef sig={SIG} url={null} />);       // local fork: plain code, no link
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByTitle(SIG)).toBeInTheDocument();
  });

  it("position cards show the buy and sell transactions with Solscan links", () => {
    const p = pos({ buys: [{ sig: SIG, usdc: 1_000_000, tokens: 1000, explorer_url: URL }],
      sells: [{ trigger: "take_profit", amount: 500, usdc: 600_000, sig: SIG, ratio: 1.2, explorer_url: URL }] });
    render(<PositionsPanel positions={[p]} canExit onEmergency={vi.fn()} testEnabled={false} />);
    expect(screen.getAllByRole("link", { name: /Solscan/ })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Test take profit" })).toBeNull();
    render(<PositionSheet positions={[p]} />);
    expect(screen.getAllByRole("link", { name: /Solscan/ }).length).toBeGreaterThanOrEqual(3);
  });

  it("the confirmation card says real funds on mainnet and nothing extra on the fork", () => {
    const { rerender } = render(<OrderSheet cards={[card()]} buttons onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.queryByTestId("real-funds-note")).toBeNull();
    rerender(<OrderSheet cards={[card()]} buttons onApprove={vi.fn()} onReject={vi.fn()} realFunds />);
    expect(screen.getByTestId("real-funds-note")).toHaveTextContent(/real Solana transaction/);
    expect(screen.getByRole("button", { name: "Approve 47" })).toBeInTheDocument();   // same approval flow, same code
  });

  it("the activity log links a filled trade to Solscan", () => {
    const state = { engine: { cards: [] }, modes: { research: "cache" } } as unknown as AppState;
    render(<LiveLog state={state} events={[{ id: 1, type: "trade_executed", ts: 1, loud: false,
      speak: "Card 47 filled: bought for 1 dollar.", panel: { side: "buy", signature: SIG, explorer_url: URL } }]} />);
    expect(screen.getByRole("link", { name: /Solscan/ })).toHaveAttribute("href", URL);
  });
});
