import { describe, expect, it, vi } from "vitest";
import { ApiError, type Api } from "../api";
import { BroadcastQueue, ECHO_WINDOW_MS, REPLY_WAIT_MS, URGENT_MAX_HOLD_MS, UserActivity, broadcastText, isEchoSpeak, shouldSpeak } from "../voice/broadcast";
import { SpeechGate, flushConversationOutput, GATE_SPEAK_GRACE_MS } from "../voice/speechGate";
import { routeText } from "../voice/textRouter";
import { makeClientTools } from "../voice/tools";
import { MAX_AGE_MS, SYSTEM_MARK, UtteranceStore } from "../voice/utterances";
import type { AppState, BackendEvent } from "../types";

function fakeApi(responses: Record<string, any> = {}) {
  const calls: { method: string; path: string; body?: any }[] = [];
  const api: Api = {
    get: async (path) => { calls.push({ method: "GET", path }); return responses[path] ?? {}; },
    post: async (path, body) => {
      calls.push({ method: "POST", path, body });
      const r = responses[path];
      if (r instanceof Error) throw r;
      return typeof r === "function" ? r(body) : r ?? { ok: true };
    },
  };
  return { api, calls };
}

const ev = (o: Partial<BackendEvent>): BackendEvent => ({ id: 1, type: "card_created", speak: "Card 47: buy 10 dollars.", panel: {}, ts: 0, loud: false, ...o });

describe("UtteranceStore", () => {
  it("ignores system updates, stores only the user's words", () => {
    const s = new UtteranceStore();
    expect(s.record(`${SYSTEM_MARK} Card 47. To approve, say "approve 47"`, "voice")).toBeNull();
    expect(s.record("[URGENT update] Warning", "voice")).toBeNull();
    expect(s.record("【紧急播报】注意", "voice")).toBeNull();
    expect(s.latest()).toBeNull();
    s.record("approve 47", "voice");
    expect(s.latest()?.text).toBe("approve 47");
  });
  it("expired or used utterances cannot approve again", () => {
    let t = 0;
    const s = new UtteranceStore(() => t);
    const u = s.record("approve 47", "voice");
    s.markUsed(u);
    expect(s.latest({ unusedOnly: true })).toBeNull();
    expect(s.latest()?.text).toBe("approve 47");
    t = MAX_AGE_MS + 1;
    expect(s.latest()).toBeNull();
  });
});

describe("BroadcastQueue", () => {
  it("does not broadcast replayed, silent or empty-speak events", () => {
    expect(shouldSpeak(ev({ replay: true }))).toBe(false);
    expect(shouldSpeak(ev({ type: "research_payment" }))).toBe(false);
    expect(shouldSpeak(ev({ speak: "" }))).toBe(false);
    expect(shouldSpeak(ev({}))).toBe(true);
  });
  it("queues while the agent speaks; urgent events jump the queue with their own marker; each event once", () => {
    const sent: string[] = [];
    let can = false;
    const q = new BroadcastQueue((t) => sent.push(t), () => can);
    q.push(ev({ id: 1 }));
    q.push(ev({ id: 2, type: "sell_failed", speak: "Sell failed", loud: true }));
    q.push(ev({ id: 1 }));
    expect(sent).toEqual([]);
    expect(q.pending).toBe(2);
    can = true;
    q.flush();
    q.flush();
    expect(sent).toEqual(["[URGENT update] Sell failed"]);   // the next waits for the agent's reply
    q.agentReplied();
    q.flush();
    expect(sent).toEqual(["[URGENT update] Sell failed", `${SYSTEM_MARK} Card 47: buy 10 dollars.`]);
    expect(broadcastText(ev({}))).toMatch(/^\[System update\] /);
  });
});

describe("client tools", () => {
  it("submit_mandate_fields sends only the user's words, never text from agent args", async () => {
    const { api, calls } = fakeApi({ "/mandate/fields": { speak: "Noted", missing_required: [], recap: { ready: true, digest: "d1" } } });
    const utter = new UtteranceStore();
    let digest: string | null = null;
    const t = makeClientTools({ api, utter, getDigest: () => digest, setDigest: (d) => (digest = d) });
    expect(JSON.parse(await t.submit_mandate_fields({ round: 1 })).ok).toBe(false);   // no utterance yet
    utter.record("50 dollars total, 10 per trade", "voice");
    const r = JSON.parse(await t.submit_mandate_fields({ round: 1, utterance: "words made up by the agent" } as any));
    expect(r).toMatchObject({ ok: true, ready: true });
    expect(calls.at(-1)).toEqual({ method: "POST", path: "/mandate/fields", body: { utterance: "50 dollars total, 10 per trade", round: 1 } });
    expect(digest).toBe("d1");
    expect(r.speak).toBe("Noted");
  });
  it("notifies onToolSpeak with the backend speak line", async () => {
    const { api } = fakeApi({ "/mandate/fields": { speak: "Got it: 5 cents total.", missing_required: [], recap: {} } });
    const utter = new UtteranceStore();
    const spoken: string[] = [];
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {}, onToolSpeak: (s) => spoken.push(s) });
    utter.record("five cents total", "voice");
    await t.submit_mandate_fields({ round: 1 });
    expect(spoken).toEqual(["Got it: 5 cents total."]);
  });
  it("when the speech gate hid the pre-tool line, the tool result tells the model to say the official speak now", async () => {
    const { api } = fakeApi({ "/mandate/fields": { speak: "Got it: 5 cents total.", missing_required: [], recap: {} } });
    const utter = new UtteranceStore();
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {}, wasGated: () => true });
    utter.record("five cents total", "voice");
    const r = JSON.parse(await t.submit_mandate_fields({ round: 1 }));
    expect(r.speak).toBe("Got it: 5 cents total.");
    expect(r.instruction).toMatch(/has not heard you this turn/i);
  });

  it("confirm_mandate carries the recap digest and the utterance", async () => {
    const { api, calls } = fakeApi({ "/mandate/confirm": { frozen: true } });
    const utter = new UtteranceStore();
    const t = makeClientTools({ api, utter, getDigest: () => "dg", setDigest: () => {} });
    utter.record("confirm", "voice");
    expect(JSON.parse(await t.confirm_mandate()).ok).toBe(true);
    expect(calls.at(-1)?.body).toEqual({ utterance: "confirm", recap_digest: "dg" });
  });

  it("respond_to_card: utterance + the agent's code; the utterance is used up after a successful approval", async () => {
    const { api, calls } = fakeApi({ "/voice/card-respond": { ok: true, status: "executed" } });
    const utter = new UtteranceStore();
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {} });
    expect(JSON.parse(await t.respond_to_card({ action: "approve", code: "47" })).ok).toBe(false);
    utter.record("approve forty-seven", "voice");
    expect(JSON.parse(await t.respond_to_card({ action: "approve", code: 47 })).ok).toBe(true);
    expect(calls.at(-1)?.body).toEqual({ action: "approve", code: "47", utterance: "approve forty-seven", amount_usdc: undefined });
    // the same sentence cannot approve a second card
    expect(JSON.parse(await t.respond_to_card({ action: "approve", code: "52" })).reason).toMatch(/user's latest words/);
    // reject needs no utterance
    expect(JSON.parse(await t.respond_to_card({ action: "reject", code: "52" })).ok).toBe(true);
    expect(JSON.parse(await t.respond_to_card({ action: "buy_everything" })).ok).toBe(false);
  });

  it("passes backend refusals to the agent without throwing", async () => {
    const { api } = fakeApi({ "/voice/card-respond": new ApiError(409, "that sentence came from a system broadcast, not from you") });
    const utter = new UtteranceStore();
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {} });
    utter.record("approve 47", "voice");
    expect(JSON.parse(await t.respond_to_card({ action: "approve", code: "47" }))).toEqual({ ok: false, reason: "that sentence came from a system broadcast, not from you" });
  });

  it("respond_to_topup / emergency_exit / get_status", async () => {
    const { api, calls } = fakeApi({ "/research/topup": { ok: true }, "/emergency-exit": { results: [{}, {}] }, "/agent/status": { phase: "monitoring positions" } });
    const utter = new UtteranceStore();
    const starts: string[] = [];
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {}, onToolStart: (n) => starts.push(n) });
    utter.record("add fifty cents", "voice");
    await t.respond_to_topup({ action: "approve", amount_usdc: 0.5 });
    expect(calls.at(-1)?.body).toEqual({ action: "approve", amount_usdc: 0.5, utterance: "add fifty cents", channel: "voice" });
    utter.record("sell all", "voice");
    expect(JSON.parse(await t.emergency_exit())).toEqual({ ok: true, sold: 2 });
    expect(calls.at(-1)?.body).toEqual({ utterance: "sell all", channel: "voice" });
    expect(JSON.parse(await t.get_status())).toEqual({ phase: "monitoring positions" });
    expect(starts).toEqual(["respond_to_topup", "emergency_exit", "get_status"]);
  });
});

describe("direct text-mode routing", () => {
  const base = (o: Partial<AppState>): AppState => ({
    mandate_session: { frozen: false, missing_required: ["budget.total_usdc"], recap: null }, phase: "mandate",
    modes: { discovery: "replay", research: "cache", ui_buttons: true }, mandate: null, candidates: [], research: [],
    spend: null, topup: null, engine: null, voice_configured: true, ...o,
  });
  it("mandate phase: fill fields first, then \"confirm\" after the recap freezes", async () => {
    const { api, calls } = fakeApi({ "/mandate/fields": { speak: "Noted" }, "/mandate/confirm": { speak: "Frozen" } });
    expect((await routeText("50 dollars total", base({}), api, null)).target).toBe("mandate");
    expect(calls.at(-1)?.body).toEqual({ utterance: "50 dollars total", round: 1 });
    const ready = base({ mandate_session: { frozen: false, missing_required: [], recap: { ready: true, missing_required: [], text: "x", digest: "dg" } } });
    expect((await routeText("confirm", ready, api, null)).target).toBe("confirm");
    expect(calls.at(-1)?.body).toEqual({ utterance: "confirm", recap_digest: "dg" });
  });
  it("with a pending card, approve / reject by code; with a top-up request, route to top-up", async () => {
    const card: any = { id: "c1", code: "47", status: "pending" };
    const { api, calls } = fakeApi({ "/voice/card-respond": { ok: true, status: "executed" }, "/research/topup": { ok: true } });
    const st = base({ mandate_session: { frozen: true }, engine: { wallet: "w", spent_usdc: 0, cards: [card], positions: [], trades: [] } });
    await routeText("approve 47", st, api, null);
    expect(calls.at(-1)).toMatchObject({ path: "/voice/card-respond", body: { action: "approve", code: "47", utterance: "approve 47" } });
    await routeText("reject 47", st, api, null);
    expect(calls.at(-1)?.body.action).toBe("reject");
    await routeText("change 47 to 5 dollars", st, api, null);
    expect(calls.at(-1)?.body).toMatchObject({ action: "modify", code: "47", amount_usdc: 5 });
    await routeText("147 approve", st, api, null);
    expect(calls.at(-1)?.body.code).toBeUndefined();
    await routeText("add 50 cents", base({ topup: { needed_usdc: 0.05, spent_usdc: 1, budget_usdc: 1, suggest_usdc: 0.5, expires_at: 0 } }), api, null);
    expect(calls.at(-1)?.body).toMatchObject({ action: "approve", amount_usdc: 0.5, utterance: "add 50 cents" });
  });
  const offer = (n: number, status = "open") => ({
    id: `r1c${n}`, number: n, round: 1, mint: `M${n}`, symbol: `S${n}`, pool: `P${n}`, dex: "pumpswap", paid_promo: false, metrics: {},
    status, card: null, market_live: true, market_at: 0, signals: { good: [], caution: [] },
  });
  const desk = (offers: any[]) => ({ offers, cap_usdc: 10, per_trade_usdc: 10, remaining_usdc: 50, round: 1 });
  it("selection desk: coin orders and skips go to /desk/*; simulate goes to the price test", async () => {
    const { api, calls } = fakeApi({
      "/desk/order": { ok: true, cards: [{ code: "47", coin_number: 1, amount_usdc: 5 }, { code: "12", coin_number: 3, amount_usdc: 5 }] },
      "/desk/skip": { ok: true, numbers: [2] }, "/test/price-move": { ok: true },
    });
    const eng = { wallet: "w", spent_usdc: 0, cards: [], positions: [], trades: [] };
    const st = base({ mandate_session: { frozen: true }, engine: eng, desk: desk([offer(1), offer(2), offer(3)]) });
    const r = await routeText("coin 1 and coin 3, 5 dollars each", st, api, null);
    expect(r.reply).toBe("Card 47: coin 1, 5 dollars; Card 12: coin 3, 5 dollars. Approve each by its code.");
    expect(calls.at(-1)).toMatchObject({ path: "/desk/order", body: { utterance: "coin 1 and coin 3, 5 dollars each", channel: "voice" } });
    expect(calls.at(-1)?.body.orders).toBeUndefined();          // the backend reads coins and amounts from the words
    await routeText("skip coin 2", st, api, null);
    expect(calls.at(-1)).toMatchObject({ path: "/desk/skip", body: { utterance: "skip coin 2" } });
    await routeText("simulate take profit", base({ mandate_session: { frozen: true }, engine: eng }), api, null);
    expect(calls.at(-1)).toMatchObject({ path: "/test/price-move", body: { kind: "take_profit", utterance: "simulate take profit" } });
    await routeText("test the stop loss", base({ mandate_session: { frozen: true }, engine: eng }), api, null);
    expect(calls.at(-1)?.body.kind).toBe("stop_loss");
  });
  it("running session: a new filter or another round goes to the mandate (never \"read-only\")", async () => {
    const { api, calls } = fakeApi({ "/mandate/fields": { speak: "Starting a new research round with these filters." }, "/mandate/confirm": { speak: "Settings updated" } });
    const eng = { wallet: "w", spent_usdc: 0, cards: [], positions: [], trades: [] };
    const st = base({ mandate_session: { frozen: true }, engine: eng, phase: "idle", desk: desk([offer(1, "bought")]) });
    const r = await routeText("another research round with market cap below 400,000", st, api, null);
    expect(r.target).toBe("amend");
    expect(calls.at(-1)).toMatchObject({ path: "/mandate/fields", body: { round: 2 } });
    const am = base({ mandate_session: { frozen: false, amending: true, amend_kind: "guards", recap: { ready: true, missing_required: [], text: "", digest: "d9" } }, engine: eng });
    await routeText("confirm", am, api, null);
    expect(calls.at(-1)).toMatchObject({ path: "/mandate/confirm", body: { utterance: "confirm", recap_digest: "d9" } });
    // while the desk is open, a "new round" sentence is not mistaken for a coin order
    const open = base({ mandate_session: { frozen: true }, engine: eng, desk: desk([offer(1)]) });
    await routeText("find new coins, research again", open, api, null);
    expect(calls.at(-1)?.path).toBe("/mandate/fields");
  });
});

describe("new client tools", () => {
  it("place_orders: sends the user's words; incomplete agent args still go to the backend; skip_coins", async () => {
    const { api, calls } = fakeApi({ "/desk/order": { ok: true, cards: [] }, "/desk/skip": { ok: true }, "/test/price-move": { ok: true } });
    const utter = new UtteranceStore();
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {}, onToolStart: () => {} });
    expect(JSON.parse(await t.place_orders({ coin_numbers: [1, 3], amount_usdc_each: 5 })).ok).toBe(false);   // no words yet
    utter.record("coin 1 and coin 3, 5 dollars each", "voice");
    await t.place_orders({ coin_numbers: [1, 3], amount_usdc_each: 5 });
    expect(calls.at(-1)).toEqual({ method: "POST", path: "/desk/order", body: {
      orders: [{ coin_number: 1, amount_usdc: 5 }, { coin_number: 3, amount_usdc: 5 }], utterance: "coin 1 and coin 3, 5 dollars each", channel: "voice" } });
    // malformed agent amounts are ignored; the backend reads the unused utterance (none here — previous one was consumed)
    expect(JSON.parse(await t.place_orders({ coin_numbers: [1, 3], amounts_usdc: [5] })).ok).toBe(false);
    utter.record("buy coin one for 0.01 dollars", "voice");
    await t.place_orders({ coin_numbers: [1] }); // no amount in the tool args
    expect(calls.at(-1)).toEqual({ method: "POST", path: "/desk/order", body: {
      utterance: "buy coin one for 0.01 dollars", channel: "voice" } });
    utter.record("coin 1 five dollars and coin 3 two dollars", "voice");
    await t.place_orders({ coin_numbers: [1, 3], amounts_usdc: [5, 2] });
    expect(calls.at(-1)?.body.orders).toEqual([{ coin_number: 1, amount_usdc: 5 }, { coin_number: 3, amount_usdc: 2 }]);
    utter.record("skip coin 2", "voice");
    await t.skip_coins({ coin_numbers: [2] });
    expect(calls.at(-1)).toMatchObject({ path: "/desk/skip", body: { coin_numbers: [2], utterance: "skip coin 2", channel: "voice" } });
    utter.record("simulate stop loss", "voice");
    await t.simulate_price_move({ kind: "stop_loss" });
    expect(calls.at(-1)?.body).toEqual({ kind: "stop_loss", utterance: "simulate stop loss", channel: "voice" });
  });
  it("confirm_mandate in a running session: no digest needed for a new round", async () => {
    const { api, calls } = fakeApi({ "/mandate/confirm": { frozen: true, amended: true, kind: "filters", speak: "Settings updated, version 2." } });
    const utter = new UtteranceStore();
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {} });
    utter.record("start research", "voice");
    const r = JSON.parse(await t.confirm_mandate());
    expect(r).toMatchObject({ ok: true, amended: true, kind: "filters", speak: "Settings updated, version 2." });
    expect(calls.at(-1)?.body).toEqual({ utterance: "start research", recap_digest: "" });
  });
});

describe("reply pacing", () => {
  it("drops a queued update when the tool result already said the same words", () => {
    const sent: string[] = [];
    let busy = true;
    const q = new BroadcastQueue((t) => sent.push(t), () => !busy);
    q.push(ev({ id: 1, type: "cards_ready", speak: 'Card 84: sell 50 percent of coin 1. To approve, say "approve 84".' }));
    q.remember('Card 84: sell 50 percent of coin 1. To approve, say "approve 84".', true);
    busy = false; q.flush();
    expect(sent).toEqual([]);
    expect(q.pending).toBe(0);
  });
  it("waits for the agent's reply before the next update, and merges queued routine updates in order", () => {
    let now = 0;
    const sent: string[] = [];
    const q = new BroadcastQueue((t) => sent.push(t), () => true, () => false, () => now);
    q.push(ev({ id: 1, type: "round_started", speak: "Mandate frozen. Looking for coins now." }));
    q.push(ev({ id: 2, type: "candidates_found", speak: "Found 52 candidate coins" }));
    q.push(ev({ id: 22, type: "trade_executed", speak: "Card 11 filled" }));
    q.push(ev({ id: 3, type: "research_done", speak: "Research done: 1 of 3 coins passed." }));
    expect(sent).toEqual(["[System update] Mandate frozen. Looking for coins now."]);
    now += 1000; q.flush();
    expect(sent).toHaveLength(1);
    q.agentReplied(); q.flush();
    expect(sent.at(-1)).toBe("[System update] Card 11 filled. Research done: 1 of 3 coins passed.");   // stale progress dropped
    q.push(ev({ id: 4, type: "desk_ready", speak: "1 coin qualified." }));
    expect(sent).toHaveLength(2);
    now += REPLY_WAIT_MS; q.flush();               // no reply came: do not stall forever
    expect(sent).toHaveLength(3);
    q.expectReply();                               // she typed to the agent
    q.push(ev({ id: 5, type: "x", speak: "Price moved." }));
    expect(sent).toHaveLength(3);
  });
});

describe("duplex", () => {
  it("system updates wait while the user is talking; an urgent one waits at most URGENT_MAX_HOLD_MS", () => {
    let now = 10_000;
    const act = new UserActivity(() => now);
    const sent: string[] = [];
    const q = new BroadcastQueue((t) => sent.push(t), () => true, () => act.talking(), () => now);
    act.vad(0.9);
    q.push(ev({ id: 1, speak: "Cards are ready." }));
    expect(sent).toEqual([]);                       // she is speaking: hold
    now += 800; act.vad(0.2); q.flush();
    expect(sent).toEqual([]);                       // a short pause is not the end of her turn
    now += 1300; q.flush();
    expect(sent).toEqual(["[System update] Cards are ready."]);
    q.agentReplied();
    act.finalTranscript();                          // a final transcript also holds for a moment
    q.push(ev({ id: 2, speak: "Bought." }));
    expect(sent.length).toBe(1);
    now += 1600; q.flush();
    expect(sent.length).toBe(2);
    // urgent: she keeps talking, but a stop-loss sale is announced after at most URGENT_MAX_HOLD_MS
    q.push(ev({ id: 3, type: "sell_failed", speak: "Stop loss sold.", loud: true }));
    for (let i = 0; i < 10; i++) { now += 500; act.vad(0.95); q.flush(); }
    expect(sent.at(-1)).toBe("[URGENT update] Stop loss sold.");
    expect(URGENT_MAX_HOLD_MS).toBeLessThanOrEqual(5000);
  });

  it("stop_research posts the latest words; emergency_exit passes the background 'selling' status through", async () => {
    const { api, calls } = fakeApi({ "/research/stop": { ok: true, status: "stopped", phase: "between rounds" },
                                     "/emergency-exit": { ok: true, status: "selling", positions: 2, note: "fills arrive as updates" } });
    const utter = new UtteranceStore();
    const t = makeClientTools({ api, utter, getDigest: () => null, setDigest: () => {} });
    utter.record("stop the research", "voice");
    expect(JSON.parse(await t.stop_research())).toMatchObject({ ok: true, status: "stopped" });
    expect(calls.at(-1)).toEqual({ method: "POST", path: "/research/stop", body: { utterance: "stop the research", channel: "voice" } });
    utter.record("sell everything", "voice");
    expect(JSON.parse(await t.emergency_exit())).toMatchObject({ ok: true, status: "selling", positions: 2 });
  });

  it("text mode: stop research while a round runs; desk orders report building cards", async () => {
    const { api, calls } = fakeApi({ "/research/stop": { ok: true, status: "stopped" },
                                     "/desk/order": { ok: true, status: "building_cards", orders: [{ coin_number: 1, amount_usdc: 5 }] } });
    const st = { mandate_session: { frozen: true }, phase: "research", round_running: true, desk: { offers: [] } } as unknown as AppState;
    expect((await routeText("stop the research", st, api, null)).target).toBe("research_stop");
    expect(calls.at(-1)?.path).toBe("/research/stop");
    const st2 = { mandate_session: { frozen: true }, phase: "selecting", round_running: false,
                  desk: { offers: [{ number: 1, status: "open" }] } } as unknown as AppState;
    expect((await routeText("coin 1, 5 dollars", st2, api, null)).reply).toBe("Building cards for coin 1…");
  });
});

describe("echo suppression", () => {
  it("treats a restatement of the same facts as an echo, but not a later fill or a new number", () => {
    const tool = "Got it: 5 cents total, at most 1 cent per trade, fee cap set to the default 0.002 SOL. My proposed coin filters are on the right.";
    expect(isEchoSpeak(tool, [tool])).toBe(true);
    expect(isEchoSpeak("[System update] " + tool, [tool])).toBe(true);
    expect(isEchoSpeak("Settings updated, version 2. New research round started.",
                       ["Settings updated, version 2. New research round started."])).toBe(true);
    expect(isEchoSpeak("Card 55 filled: bought for 0.01 dollars. Now watching stop loss and take profit.",
                       ["Buying now."])).toBe(false);
    expect(isEchoSpeak("Card 12: coin 2, 5 dollars.", ["Card 47: coin 1, 5 dollars."])).toBe(false);
  });
  it("does not broadcast a system update that only echoes a recent tool speak, or a covered type after a tool speak", () => {
    const sent: string[] = [];
    const q = new BroadcastQueue((t) => sent.push(t), () => true);
    q.remember("Got it: 5 cents total, at most 1 cent per trade, fee cap set to the default 0.002 SOL.", true);
    q.push(ev({ id: 10, type: "mandate_updated", speak: "Got it: 5 cents total, at most 1 cent per trade, fee cap set to the default 0.002 SOL." }));
    expect(sent).toEqual([]);
    q.push(ev({ id: 11, type: "mandate_amended", speak: "Settings updated, version 2. Looking for coins with the new filters now." }));
    expect(sent).toEqual([]);   // covered type after a tool speak, even if the wording differs
    q.push(ev({ id: 12, type: "desk_ready", speak: "1 coin qualified; it is on the selection desk as coin 1." }));
    expect(sent).toEqual(["[System update] 1 coin qualified; it is on the selection desk as coin 1."]);
  });
  it("still announces a new result after a short acknowledgement, and never drops an urgent update", () => {
    const sent: string[] = [];
    const q = new BroadcastQueue((t) => sent.push(t), () => true);
    q.remember("Buying now.", true);
    q.push(ev({ id: 20, type: "swap_filled", speak: "Card 55 filled: bought for 0.01 dollars. Now watching stop loss and take profit." }));
    expect(sent).toHaveLength(1);
    q.remember("Card 55 filled: bought for 0.01 dollars.", true);
    q.push(ev({ id: 21, type: "sell_filled", speak: "Stop loss sold coin 1.", loud: true }));
    expect(sent.at(-1)).toBe("[URGENT update] Stop loss sold coin 1.");
    expect(ECHO_WINDOW_MS).toBeLessThanOrEqual(15_000);
  });
  it("first freeze broadcast is kept when the tool did not return a speak line", () => {
    const sent: string[] = [];
    const q = new BroadcastQueue((t) => sent.push(t), () => true);
    q.remember("One moment.");
    q.push(ev({ id: 30, type: "mandate_frozen", speak: "Mandate frozen, ID 889787. Looking for coins now." }));
    expect(sent).toEqual(["[System update] Mandate frozen, ID 889787. Looking for coins now."]);
  });
});

describe("speech gate", () => {
  it("hides the guessed pre-tool line and unmutes only after the tool returns", () => {
    const vols: number[] = [];
    const flushes: number[] = [];
    const g = new SpeechGate((v) => vols.push(v), () => flushes.push(1));
    g.arm();
    expect(vols.at(-1)).toBe(0);
    expect(g.displayAgent("Got it. Five dollars total, one dollar per trade.")).toBe(false);
    expect(g.swallowed[0]).toMatch(/Five dollars/);
    g.onToolStart();
    g.release();
    expect(flushes).toEqual([1]);
    expect(vols.at(-1)).toBe(1);
    expect(g.armed).toBe(false);
    expect(g.displayAgent("Got it: five cents total, at most one cent per trade.")).toBe(true);
  });
  it("unmutes if the agent speaks but no tool arrives (clarifying question)", () => {
    vi.useFakeTimers();
    const vols: number[] = [];
    const g = new SpeechGate((v) => vols.push(v), () => {});
    g.arm();
    expect(g.displayAgent("How much for coin one?")).toBe(false);
    vi.advanceTimersByTime(GATE_SPEAK_GRACE_MS);
    expect(g.armed).toBe(false);
    expect(vols.at(-1)).toBe(1);
    vi.useRealTimers();
  });
  it("flushConversationOutput bumps lastInterruptTimestamp and interrupts output", () => {
    const calls: number[] = [];
    const conv = { currentEventId: 17, lastInterruptTimestamp: 0, output: { interrupt: (ms?: number) => calls.push(ms ?? 0) } };
    flushConversationOutput(conv);
    expect(conv.lastInterruptTimestamp).toBe(18);
    expect(calls).toEqual([80]);
    flushConversationOutput(null);
    expect(calls).toEqual([80]);
  });
});

vi.useRealTimers();

describe("P0: idle mute and plain broadcasts", () => {
  it("IDLE mute survives a barge-in and only lifts when addressed", () => {
    const vols: number[] = [];
    const g = new SpeechGate((v) => vols.push(v), () => {});
    g.muteIdle();
    expect(vols.at(-1)).toBe(0);
    expect(g.displayAgent("Sure, two lattes.")).toBe(false);   // a reply to side talk stays off screen
    g.disarm();                                                  // barge-in
    expect(vols.at(-1)).toBe(0);
    g.unmuteIdle();
    expect(vols.at(-1)).toBe(1);
    expect(g.displayAgent("Card 47 is ready.")).toBe(true);
  });

  it("broadcasts carry no quotation marks", () => {
    const ev = { id: 1, type: "desk_ready", speak: 'Pick any, for example "coin 1, 1 cent".', panel: {}, ts: 0, loud: false } as unknown as BackendEvent;
    expect(broadcastText(ev)).toBe("[System update] Pick any, for example coin 1, 1 cent.");
  });
});

describe("P1 text routes: co-pilot", () => {
  const base = (o: Partial<AppState>): AppState => ({
    mandate_session: { frozen: false, missing_required: [], recap: null }, phase: "selecting",
    modes: { discovery: "replay", research: "cache", ui_buttons: false }, mandate: null, candidates: [], research: [],
    spend: null, topup: null, engine: null, voice_configured: true, ...o,
  });
  const offer = (n: number) => ({ id: `r1c${n}`, number: n, round: 1, mint: `M${n}`, symbol: "", pool: `P${n}`, dex: "pumpswap",
    paid_promo: false, metrics: {}, status: "open", card: null, market_live: true, market_at: 0, signals: { good: [], caution: [] } });
  const desk = (offers: any[]) => ({ offers, cap_usdc: 1, per_trade_usdc: 1, remaining_usdc: 5, round: 1 });
  it("stop question, yes to a de-risk offer, and side talk is ignored", async () => {
    const { api, calls } = fakeApi({ "/desk/stop": { ok: true, speak: "Coin 1 usually swings about 7 percent, so I'd put the stop at about 12 percent." },
                                      "/voice/card-respond": { ok: true, status: "approved" } });
    const card = { id: "c1", code: "47", status: "pending", offer: "Coin 1 is up 12 percent. Want to sell half?" } as any;
    const eng = { wallet: "w", spent_usdc: 0, cards: [card], positions: [], trades: [] };
    const st = base({ mandate_session: { frozen: true }, engine: eng, desk: desk([offer(1)]) });
    const r = await routeText("where should my stop be?", st, api, null);
    expect(r.target).toBe("stop_suggest");
    expect(r.reply).toContain("stop at about 12 percent");
    const y = await routeText("yes", st, api, null);
    expect(y.target).toBe("card");
    expect(calls.at(-1)).toMatchObject({ path: "/voice/card-respond", body: { action: "approve", utterance: "yes" } });
    expect(calls.at(-1)?.body.code).toBeUndefined();
    const n = calls.length;
    const side = await routeText("can I get two lattes please", st, api, null);
    expect(side.target).toBe("ignored");
    expect(calls.length).toBe(n);
  });
});
