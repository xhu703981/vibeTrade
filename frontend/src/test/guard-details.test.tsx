import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GuardDetails, type GuardView } from "../ui/GuardDetails";
import { GuardSheet, OrderSheet } from "../ui/sheets";
import type { Card, BackendEvent } from "../types";
const state = '{"mandate_allows":{"input":"USDC (input)","output":"target token (output)"}}';
const view: GuardView = {
  guard_run_id: "run1", transaction_sha256: "digest", card_id: "card1", tx_bytes_len: 523, tx_base64_prefix: "base64prefix",
  verdict: "allow", decoded: [{ index: 0, program: "ComputeBudget", instruction: "SetComputeUnitLimit", allowed: true }],
  lookup_tables: { tables: 2, accounts_resolved: 14 }, simulation: { source: "surfnet_profileTransaction", scope: "profiled accounts", success: true,
    balances: [{ asset: "USDC", unit: "base units", before: 5_000_000, after: 4_000_000, delta: -1_000_000 },
      { asset: "target token", unit: "base units", before: null, after: null, delta: null }] },
  structured_json: state, structured_status: "sent_to_jev", rules: [{ name: "Fee within mandate cap", result: "pass" }],
  jev: { question: { instructions: "Is this a plain swap?" }, risk: 0.06, threshold: 0.5, status: "answered" },
};
const open = () => fireEvent.click(screen.getByRole("button", { name: "Show what the checks found" }));
describe("Read-only Guard details", () => {
  it("loads only on expansion: six sections, exact input, actual risk and no POST", async () => {
    const api = { get: vi.fn().mockResolvedValue(view), post: vi.fn() };
    const { container } = render(<GuardDetails api={api} runId="run1" cardId="card1" code="51" />);
    expect(api.get).not.toHaveBeenCalled(); open(); await screen.findByText("523 bytes");
    expect(api.get).toHaveBeenCalledWith("/cards/51/guard_view?card_id=card1&guard_run_id=run1");
    expect(screen.getAllByRole("heading").map((h) => h.textContent)).toEqual([
      "1. Transaction bytes", "2. Decoded instructions", "3. Simulated balances", "4. Structured view sent to JEV",
      "5. Rules actually checked", "6. JEV and final verdict"]);
    expect(screen.getByLabelText("Exact JEV state JSON").textContent).toBe(state);
    expect(screen.getByText("No addresses. No token names.")).toBeTruthy();
    expect(screen.getByText("Lookup tables: 14 accounts resolved (2 tables)")).toBeTruthy();
    expect(screen.getByText("5,000,000 to 4,000,000")).toBeTruthy();
    expect(screen.getByText("Unavailable to Unavailable")).toBeTruthy();
    expect(screen.getByText("Risk: 0.060 · threshold 0.5")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "JEV risk" }).getAttribute("aria-valuenow")).toBe("0.06");
    expect((container.querySelector(".guard-risk-mark") as HTMLElement).style.left).toBe("50%");
    expect(screen.getByText("Passed")).toBeTruthy(); expect(api.post).not.toHaveBeenCalled();
  });
  it("shows rule-blocked, not sent, no invented meter", async () => {
    const api = { get: vi.fn().mockResolvedValue({ ...view, card_id: undefined, verdict: "block", structured_json: null,
      structured_status: "not_sent", jev: { not_asked: "blocked by rules" }, rules: [{ name: "Input within budget", result: "block" }],
      decoded: [{ index: 0, program: "SPL Token", instruction: "Transfer", allowed: false }] }), post: vi.fn() };
    render(<GuardDetails api={api} runId="run1" />); open(); await screen.findByText("Not asked: blocked by rules");
    expect(screen.getByText("Not sent. Blocked by rules before JEV.")).toBeTruthy();
    expect(screen.getByText("Not allowed")).toBeTruthy(); expect(screen.queryByRole("meter")).toBeNull();
    expect(screen.queryByLabelText("Exact JEV state JSON")).toBeNull();
    expect(api.get).toHaveBeenCalledWith("/guard-runs/run1/guard_view");
  });
  it("handles legacy, loading and errors honestly", async () => {
    const api = { get: vi.fn().mockRejectedValue(new Error("unsafe backend text")), post: vi.fn() };
    const { rerender } = render(<GuardDetails api={api} />); open();
    expect(screen.getByText(/Evidence unavailable for this card/)).toBeTruthy(); expect(api.get).not.toHaveBeenCalled();
    rerender(<GuardDetails api={api} runId="run1" />); open(); await screen.findByText(/Evidence unavailable: the capture is missing/);
    expect(screen.queryByText("unsafe backend text")).toBeNull();
    const pending = { get: vi.fn(() => new Promise<any>(() => {})), post: vi.fn() };
    rerender(<GuardDetails api={pending} runId="run2" />); open(); expect(screen.getByText("Loading the recorded checks…")).toBeTruthy();
  });
  it("never shows a different run or a stale replacement response", async () => {
    let resolve!: (d: any) => void;
    const api = { get: vi.fn(() => new Promise<any>((r) => { resolve = r; })), post: vi.fn() };
    const { rerender } = render(<GuardDetails api={api} runId="run1" cardId="card1" code="51" />); open();
    rerender(<GuardDetails api={api} runId="run2" cardId="card2" code="52" />); resolve(view);
    await waitFor(() => expect(screen.queryByText("523 bytes")).toBeNull()); expect(screen.queryByRole("heading")).toBeNull();
    const mismatched = { get: vi.fn().mockResolvedValue(view), post: vi.fn() };
    rerender(<GuardDetails api={mismatched} runId="run2" cardId="card2" code="52" />); open();
    await screen.findByText(/does not match this card/); expect(screen.queryByText("523 bytes")).toBeNull();
  });
  it("both approval cards and blocked panels expose the expansion", () => {
    const card = { id: "c", code: "51", guard: { decision: "allow", reasons: [], jev_risk: 0.06 }, amount_usdc: 1,
      coin_number: 1, kind: "buy", created_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 180 } as unknown as Card;
    const { unmount } = render(<OrderSheet cards={[card]} buttons onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Show what the checks found" })).toBeTruthy(); unmount();
    const event = { id: 1, type: "guard_blocked", speak: "Nothing was sent.", panel: { reasons: ["old unstructured reason"] } } as unknown as BackendEvent;
    render(<GuardSheet event={event} />); open(); expect(screen.getByText(/Evidence unavailable for this card/)).toBeTruthy();
    expect(screen.queryByText("old unstructured reason")).toBeNull();
  });
});
