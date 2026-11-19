import { describe, expect, it } from "vitest";
import { EMPTY_MEMORY, availableSheets, closeSheet, coinMentions, focusCoin, nextSheet, openSheet, pillRow, type Line } from "../ui/context";
import { levelOf } from "../ui/VoiceLine";
import { sparkPath } from "../ui/Sparkline";

const offer = (n: number, status = "open") => ({ id: `o${n}`, number: n, round: 1, mint: `m${n}`, symbol: `S${n}`, pool: `p${n}`, dex: null, paid_promo: false,
  metrics: {}, status, card: null, market_live: true, market_at: null, signals: { good: [], caution: [] } });
const base = (over: any = {}): any => ({
  mandate_session: { frozen: true, amending: false, amend_changes: [], amend_kind: "none" },
  phase: "selecting", modes: { discovery: "replay", research: "cache", ui_buttons: true }, mandate: null, candidates: [], research: [], spend: null,
  topup: null, engine: { wallet: "w", spent_usdc: 0, cards: [], positions: [], trades: [] }, voice_configured: true,
  desk: { offers: [offer(1), offer(2), offer(3)], cap_usdc: 2, per_trade_usdc: 2, remaining_usdc: 10, round: 1 }, round: 1, ...over,
});
const NOW = 1_800_000_000;
const line = (who: Line["who"], text: string, ago = 1): Line => ({ who, text, at: (NOW - ago) * 1000 });

describe("coin mentions", () => {
  it("reads single and grouped coin numbers", () => {
    expect(coinMentions("Coin 2 has a worry")).toEqual([2]);
    expect(coinMentions("coins 1 and 3 look fine")).toEqual([1, 3]);
    expect(coinMentions("nothing here")).toEqual([]);
  });
  it("focus = the newest spoken line naming exactly one coin on the desk", () => {
    const offers = base().desk.offers;
    expect(focusCoin([line("agent", "Coin 2 usually swings about 14 percent.")], offers, NOW)?.coin).toBe(2);
    expect(focusCoin([line("agent", "Coins 1 and 2 passed.")], offers, NOW)).toBeNull();
    expect(focusCoin([line("agent", "Card 91: coin 1, one dollar.")], offers, NOW)).toBeNull();
    expect(focusCoin([line("agent", "Coin 2 is fine.", 120)], offers, NOW)).toBeNull();
    expect(focusCoin([line("agent", "Coin 9 is fine.")], offers, NOW)).toBeNull();
  });
});

describe("sheet choice", () => {
  it("picks pop once per round and stay until closed; a closed sheet becomes a pill", () => {
    const st = base();
    let avail = availableSheets(st, [], [], NOW);
    let m = nextSheet(EMPTY_MEMORY, avail);
    expect(m.current).toBe("picks");
    m = closeSheet(m, avail);
    m = nextSheet(m, avail);
    expect(m.current).toBeNull();
    expect(pillRow(avail, m.current).map((p) => p.key)).toContain("picks");
    m = openSheet(m, "picks");
    expect(nextSheet(m, avail).current).toBe("picks");
  });

  it("a pending card takes over, and when it resolves the sheet goes away", () => {
    const card = { id: "c1", code: "91", status: "pending", guard: { decision: "allow", reasons: [] } };
    let m = nextSheet(EMPTY_MEMORY, availableSheets(base(), [], [], NOW));
    const withCard = base({ engine: { wallet: "w", spent_usdc: 0, cards: [card], positions: [], trades: [] } });
    m = nextSheet(m, availableSheets(withCard, [], [], NOW));
    expect(m.current).toBe("order");
    const done = base({ engine: { wallet: "w", spent_usdc: 0, cards: [{ ...card, status: "executed" }], positions: [], trades: [] } });
    m = nextSheet(m, availableSheets(done, [], [], NOW));
    expect(m.current).toBeNull();
  });

  it("talking about one coin opens its chart, which folds away when the talk moves on", () => {
    let m = closeSheet(nextSheet(EMPTY_MEMORY, availableSheets(base(), [], [], NOW)), availableSheets(base(), [], [], NOW));
    const lines = [line("user", "where would you put the stop on coin 2?")];
    m = nextSheet(m, availableSheets(base(), [], lines, NOW));
    expect(m.current).toBe("coin:2");
    m = nextSheet(m, availableSheets(base(), [], lines, NOW + 120));
    expect(m.current).toBeNull();
  });

  it("a coin mention never covers a card waiting for approval", () => {
    const card = { id: "c1", code: "91", status: "pending", guard: { decision: "allow", reasons: [] } };
    const st = base({ engine: { wallet: "w", spent_usdc: 0, cards: [card], positions: [], trades: [] } });
    let m = nextSheet(EMPTY_MEMORY, availableSheets(st, [], [], NOW));
    expect(m.current).toBe("order");
    m = nextSheet(m, availableSheets(st, [], [line("agent", "Coin 3 looks calmer.")], NOW));
    expect(m.current).toBe("order");
  });

  it("guard blocks and limit confirms come first", () => {
    const ev = { id: 7, type: "guard_blocked", speak: "Blocked.", panel: {}, ts: NOW - 5, loud: true };
    const amend = base({ mandate_session: { frozen: true, amending: true, amend_kind: "guards", amend_changes: ["budget.total_usdc"] } });
    const avail = availableSheets(amend, [ev], [], NOW);
    expect(avail[0].kind).toBe("limits");
    expect(avail[1].kind).toBe("guard");
    expect(nextSheet(EMPTY_MEMORY, avail).current).toBe("limits");
    const old = availableSheets(base(), [{ ...ev, ts: NOW - 600 }], [], NOW);
    expect(old.find((a) => a.kind === "guard")?.trigger).toBeUndefined();
  });

  it("a fill shows the position sheet briefly", () => {
    const pos = { mint: "m1", symbol_display: "S1", initial_tokens: 10, held_tokens: 10, entry_usdc: 1e6, realized_usdc: 0, peak_ratio: 1, last_ratio: 1, status: "open", feed_down: false, sells: [] };
    const st = base({ phase: "monitoring", desk: { ...base().desk, offers: [offer(1, "bought")] }, engine: { wallet: "w", spent_usdc: 1, cards: [], positions: [pos], trades: [] } });
    const fill = { id: 9, type: "trade_executed", speak: "Card 91 filled.", panel: {}, ts: NOW - 2, loud: false };
    let m = nextSheet(EMPTY_MEMORY, availableSheets(st, [fill], [], NOW));
    expect(m.current).toBe("position");
    // the agent then says "Bought coin 1 ...": the fill sheet stays
    m = nextSheet(m, availableSheets(st, [fill], [line("agent", "Bought coin 1 for one dollar.")], NOW));
    expect(m.current).toBe("position");
    m = nextSheet(m, availableSheets(st, [fill], [], NOW + 300));
    expect(m.current).toBeNull();
  });
});

describe("orb + sparkline helpers", () => {
  it("level is 0..1", () => {
    expect(levelOf(undefined)).toBe(0);
    expect(levelOf(new Uint8Array([255, 255, 0, 0]))).toBe(1);
  });
  it("sparkline colours by direction", () => {
    expect(sparkPath([1, 2, 3], 100, 30).className).toBe("up");
    expect(sparkPath([3, 2, 1], 100, 30).className).toBe("down");
  });
});
