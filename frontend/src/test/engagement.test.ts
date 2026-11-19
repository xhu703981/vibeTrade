import { describe, expect, it } from "vitest";
import { addressedDeterministic, Engagement, hasWakeName } from "../voice/engagement";
import { makeClientTools, type ToolDeps } from "../voice/tools";
import { UtteranceStore } from "../voice/utterances";

describe("engagement", () => {
  it("wakes on name variants and ignores coffee in IDLE", () => {
    for (const s of ["Vibe", "hey Vibe", "Vibe Trader", "vibes", "hey vibe trader"]) {
      expect(hasWakeName(s)).toBe(true);
    }
    expect(addressedDeterministic("two coffees please", "idle")).toBe(false);
    expect(addressedDeterministic("Vibe, how are my positions?", "idle")).toBe(true);
  });

  it("stays ENGAGED for a follow-up without the name, then times out to IDLE", () => {
    let now = 1_000;
    const e = new Engagement(25_000, () => now);
    const first = e.onUserText("Vibe, how are my positions?");
    expect(first.addressed).toBe(true);
    expect(e.mode).toBe("engaged");
    const follow = e.onUserText("and how is coin 1?");
    expect(follow.addressed).toBe(true);
    now += 26_000;
    expect(e.tick()).toBe("timeout");
    expect(e.mode).toBe("idle");
    expect(e.onUserText("two coffees please").addressed).toBe(false);
  });

  it("blocks tools when not addressed", async () => {
    const api = { post: async () => { throw new Error("should not be called"); }, get: async () => ({}) };
    const utter = new UtteranceStore();
    utter.record("two coffees please", "voice");
    const deps: ToolDeps = {
      api: api as any, utter, getDigest: () => null, setDigest: () => {},
      isAddressed: () => false,
    };
    const tools = makeClientTools(deps);
    const r = JSON.parse(await tools.place_orders({ coin_numbers: [1], amount_usdc_each: 0.01 }));
    expect(r.ok).toBe(false);
    expect(r.silent).toBe(true);
    const skip = JSON.parse(await tools.skip_turn());
    expect(skip.ok).toBe(true);
  });
});

describe("engagement timing", () => {
  it("waits for an answer after a question and holds while someone talks", () => {
    let t = 0;
    const e = new Engagement(25_000, () => t);
    e.agentLine("Do you want to change any of these settings?");
    t = 40_000;
    expect(e.tick()).toBeNull();
    expect(e.onUserText("I don't want to change anything. Let's proceed.").addressed).toBe(true);
    e.agentLine("Starting research now.");
    t = 60_000; e.hold();
    t = 80_000;
    expect(e.tick()).toBeNull();
    t = 86_000;
    expect(e.tick()).toBe("timeout");
  });
});

describe("answer to the agent's question", () => {
  it("a late side-talk verdict cannot drop her reply to our question", () => {
    const e = new Engagement(25_000, () => 0);
    e.agentLine("Do you want to change any of these settings?");
    expect(e.onUserText("No, let's proceed.").addressed).toBe(true);
    expect(e.answeredQuestion).toBe(true);
    e.markSideTalk();
    expect(e.mode).toBe("engaged");
    e.agentLine("Starting research.");
    e.onUserText("hmm let me think about lunch");
    expect(e.answeredQuestion).toBe(false);
    e.markSideTalk();
    expect(e.mode).toBe("idle");
  });
});

describe("pending confirmation cards", () => {
  it("'approve 83' reaches the agent without the name while card 83 waits, and holds against a side-talk verdict", () => {
    const e = new Engagement(25_000, () => 0);
    expect(e.mode).toBe("idle");
    e.pendingCodes = ["83"];
    expect(e.onUserText("approve 83").addressed).toBe(true);
    e.markSideTalk();
    expect(e.mode).toBe("engaged");
    const f = new Engagement(25_000, () => 0);
    f.pendingCodes = ["83"];
    expect(f.onUserText("approve 47").addressed).toBe(false);   // not a card on screen: still needs the name
    expect(f.onUserText("two lattes please").addressed).toBe(false);
  });
});
