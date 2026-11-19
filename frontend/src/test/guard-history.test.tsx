import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { CardsPanel, CardView } from "../panels/CardsPanel";
import { GuardAlert } from "../panels/GuardAlert";
import { LiveLog } from "../panels/LiveLog";
import { GuardSheet } from "../ui/sheets";
import { GuardRunPage } from "../ui/GuardRunPage";
import type { GuardView } from "../ui/GuardDetails";
import type { AppState, BackendEvent, Card } from "../types";

const normal: GuardView = {
  guard_run_id: "normal-run", card_id: "normal-card", card_code: "14", transaction_sha256: "normal-digest",
  tx_bytes_len: 686, tx_base64_prefix: "normal-prefix", verdict: "allow", decoded: [], lookup_tables: { tables: 1, accounts_resolved: 16 },
  simulation: null, structured_json: "{}", structured_status: "sent_to_jev", rules: [{ name: "Actual normal check", result: "pass" }],
  jev: { risk: 0.12, threshold: 0.5, question: { instructions: "Is this a plain swap?" } },
};
const blocked: GuardView = { ...normal, guard_run_id: "blocked-run", card_id: undefined, card_code: undefined,
  verdict: "block", tx_bytes_len: 733, structured_json: null, structured_status: "not_sent",
  rules: [{ name: "Actual blocked check", result: "block" }], jev: { not_asked: "blocked by rules" } };
const card: Card = {
  id: "normal-card", code: "14", mint: "fixture", symbol_display: "Fixture", coin_number: 1, amount_usdc: 0.5, expected_out: 1,
  created_at: 1000, expires_at: 1180, seconds_left: 0, status: "expired", metrics: {},
  sim: { token_out: 1, price_impact_pct: 0.1, usdc_in: 500000, sol_change_lamports: -5000 },
  guard: { decision: "allow", reasons: [], jev_risk: 0.12, guard_run_id: "normal-run" },
};
const blockEvent: BackendEvent = { id: 22, type: "guard_blocked", ts: 1100, loud: true,
  speak: "Blocked. Nothing was sent.", panel: { guard_run_id: "blocked-run" } };
function mockReads() {
  const f = vi.fn(async (url: string, init: RequestInit) => {
    expect(init.method).toBe("GET");
    const isNormal = url === "/api/cards/14/guard_view?card_id=normal-card&guard_run_id=normal-run" || url === "/api/guard-runs/normal-run/guard_view";
    expect(isNormal || url === "/api/guard-runs/blocked-run/guard_view").toBe(true);
    return { ok: true, json: async () => isNormal ? normal : blocked };
  });
  vi.stubGlobal("fetch", f); return f;
}
const expand = (el: HTMLElement) => fireEvent.click(within(el).getByRole("button", { name: "Show what the checks found" }));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); window.history.replaceState({}, "", "/"); });

describe("Exact-run Guard history navigation", () => {
  it("opens an expired normal card's own evidence after a newer block, not the global block", async () => {
    const f = mockReads(), approve = vi.fn(), reject = vi.fn();
    render(<><CardsPanel cards={[card]} buttonsEnabled onApprove={approve} onReject={reject} /><GuardSheet event={blockEvent} /></>);
    fireEvent.click(screen.getByRole("button", { name: "Show 1 past card" }));
    const normalCard = screen.getByRole("article", { name: "Card 14" });
    expand(normalCard);
    await within(normalCard).findByText("686 bytes");
    expect(within(normalCard).getByText("Risk: 0.120 · threshold 0.5")).toBeTruthy();
    expect(within(normalCard).queryByText("Not asked: blocked by rules")).toBeNull();
    expect(within(normalCard).queryByRole("button", { name: "Approve 14" })).toBeNull();
    expand(screen.getByRole("group", { name: "Guard checks for run blocked-run" }));
    await screen.findByText("Not asked: blocked by rules");
    expect(within(normalCard).getByText("686 bytes")).toBeTruthy();
    expect(f).toHaveBeenCalledTimes(2); expect(approve).not.toHaveBeenCalled(); expect(reject).not.toHaveBeenCalled();
  });

  it("keeps the normal capture readable as the same card times out, with no approval revival", async () => {
    mockReads(); const actions = { buttonsEnabled: true, onApprove: vi.fn(), onReject: vi.fn() };
    const pending = { ...card, status: "pending" } as Card;
    const { rerender } = render(<CardView card={pending} now={1100} {...actions} />);
    expand(screen.getByRole("article", { name: "Card 14" })); await screen.findByText("686 bytes");
    rerender(<CardView card={card} now={1200} {...actions} />);
    expect(screen.getByText("686 bytes")).toBeTruthy(); expect(screen.getByText(/timed out, nothing was bought/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve 14" })).toBeNull();
  });

  it("opens the retained blocked event after a newer passed card and scopes simulation rows to their card", async () => {
    mockReads();
    const state = { engine: { cards: [card] }, modes: { research: "cache" } } as unknown as AppState;
    render(<LiveLog state={state} events={[blockEvent, { id: 23, type: "card_created", ts: 1200, loud: false,
      speak: "Card 14 passed.", panel: { card } }]} />);
    const blockRow = screen.getByLabelText("Guard blocked event 22"); expand(blockRow);
    await within(blockRow).findByText("733 bytes");
    expect(within(blockRow).getByText("Not asked: blocked by rules")).toBeTruthy();
    const simulation = screen.getByLabelText("Card 14 simulation"); expand(simulation);
    await within(simulation).findByText("686 bytes");
    expect(within(simulation).queryByText("Actual blocked check")).toBeNull();
    expect(within(blockRow).queryByRole("meter")).toBeNull();
  });

  it("binds the Activity alert expansion to its own blocked event", async () => {
    const f = mockReads(); render(<GuardAlert event={blockEvent} onDismiss={vi.fn()} />);
    expand(screen.getByRole("alert")); await screen.findByText("733 bytes");
    expect(f.mock.calls[0][0]).toBe("/api/guard-runs/blocked-run/guard_view");
  });

  it("opens exact-run links after a session reset without loading voice, state or mandate APIs", async () => {
    const f = mockReads(); window.history.replaceState({}, "", "/?guard_run_id=normal-run");
    render(<App />); await screen.findByText("686 bytes");
    expect(screen.getByText("Risk: 0.120 · threshold 0.5")).toBeTruthy();
    expect(f).toHaveBeenCalledTimes(1); expect(f.mock.calls[0][0]).toBe("/api/guard-runs/normal-run/guard_view");
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
  });

  it("switches standalone normal/block links in either direction without stale content", async () => {
    mockReads(); const { rerender } = render(<GuardRunPage runId="normal-run" />);
    await screen.findByText("686 bytes"); rerender(<GuardRunPage runId="blocked-run" />);
    await screen.findByText("733 bytes"); expect(screen.queryByText("686 bytes")).toBeNull();
    expect(screen.queryByRole("meter")).toBeNull(); rerender(<GuardRunPage runId="normal-run" />);
    await screen.findByText("686 bytes"); expect(screen.queryByText("733 bytes")).toBeNull();
  });

  it("does not request invalid run IDs or substitute another capture for a missing one", async () => {
    const f = vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }); vi.stubGlobal("fetch", f);
    const { rerender } = render(<GuardRunPage runId="invalid/run" />);
    expect(screen.getByText("Invalid Guard run ID. Nothing was requested.")).toBeTruthy(); expect(f).not.toHaveBeenCalled();
    rerender(<GuardRunPage runId="normal-run" />);
    await screen.findByText(/Evidence unavailable: the capture is missing/); expect(f).toHaveBeenCalledTimes(1);
  });
});
