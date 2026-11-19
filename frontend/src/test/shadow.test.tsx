import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HearingStrip } from "../App";
import { PARTIAL_DEBOUNCE_MS, ShadowStream } from "../voice/shadow";

describe("ShadowStream", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("throttles partials to one POST per window with the latest words; finals go at once and cancel pending partials", async () => {
    const posts: { text: string; final: boolean; seq: number }[] = [];
    const results: boolean[] = [];
    const s = new ShadowStream(async (b) => { posts.push(b); return { ok: true, hearing: null }; }, (_r, final) => results.push(final));
    s.partial("another");
    s.partial("another round with");
    s.partial("another round with market cap");
    expect(posts).toEqual([]);
    vi.advanceTimersByTime(PARTIAL_DEBOUNCE_MS);
    expect(posts).toEqual([{ text: "another round with market cap", final: false, seq: 1 }]);
    s.partial("another round with market cap");          // unchanged text: nothing new to send
    vi.advanceTimersByTime(PARTIAL_DEBOUNCE_MS);
    expect(posts.length).toBe(1);
    s.partial("another round with market cap below 300");
    s.final("Another round with market cap below 300,000.");
    vi.advanceTimersByTime(PARTIAL_DEBOUNCE_MS * 2);
    expect(posts.slice(1)).toEqual([{ text: "Another round with market cap below 300,000.", final: true, seq: 2 }]);
    await vi.runAllTimersAsync();
    expect(results).toEqual([false, true]);
    expect(PARTIAL_DEBOUNCE_MS).toBeGreaterThanOrEqual(150);
    expect(PARTIAL_DEBOUNCE_MS).toBeLessThanOrEqual(300);
  });

  it("never streams our own broadcasts and survives a backend error", async () => {
    const posts: string[] = [];
    const s = new ShadowStream(async (b) => { posts.push(b.text); throw new Error("down"); });
    s.final("[System update] Card 47: coin 1, 5 dollars.");
    s.partial("[URGENT update] Stop loss");
    vi.advanceTimersByTime(PARTIAL_DEBOUNCE_MS);
    s.final("sell all");
    await vi.runAllTimersAsync();
    expect(posts).toEqual(["sell all"]);
  });
});

describe("HearingStrip", () => {
  it("shows Listening while she talks and the staged reading; hides old readings", () => {
    const at = 1000;
    const { rerender } = render(<HearingStrip hearing={{ kind: "filter_change", label: "market cap < $300k", final: false, at }}
                                              talking={() => true} now={() => (at + 1) * 1000} />);
    expect(screen.getByText("Listening…")).toBeTruthy();
    expect(screen.getByText("Hearing: market cap < $300k…")).toBeTruthy();
    rerender(<HearingStrip hearing={{ kind: "filter_change", label: "market cap < $300k", final: false, at }}
                           talking={() => false} now={() => (at + 30) * 1000} />);
    expect(screen.queryByLabelText("Shadow listener")).toBeNull();
  });
});
