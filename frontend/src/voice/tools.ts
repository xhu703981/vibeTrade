// ElevenLabs client tools: run in the browser and call the local FastAPI directly.
// Structured arguments come from the agent; the user's raw words (utterance) are attached here from the UtteranceStore; the agent cannot pass them.
// Returns a JSON string containing only backend numbers, codes and templated text.
import { ApiError, type Api } from "../api";
import type { UtteranceStore } from "./utterances";

export type ToolDeps = {
  api: Api;
  utter: UtteranceStore;
  getDigest: () => string | null;
  setDigest: (d: string | null) => void;
  onToolStart?: (name: string) => void;
  onToolEnd?: (name: string) => void;
  onToolSpeak?: (speak: string) => void;
  /** True when the client muted/hid the pre-tool line, so the official speak must be said now. */
  wasGated?: () => boolean;
  /** False when the latest user turn was not addressed to the agent (IDLE, no name / side talk). */
  isAddressed?: () => boolean;
};

const NO_UTTERANCE = { ok: false, reason: "I did not get the user's latest words; ask the user to say it again" };

function out(x: unknown) {
  return JSON.stringify(x);
}

async function guarded(deps: ToolDeps, name: string, fn: () => Promise<unknown>): Promise<string> {
  if (name !== "skip_turn" && deps.isAddressed && !deps.isAddressed()) {
    return out({ ok: false, silent: true,
      instruction: "The user was not talking to you. Call skip_turn if you have it. Say nothing and call no other tool." });
  }
  deps.onToolStart?.(name);
  try {
    const r = await fn();
    const speak = r && typeof r === "object" && typeof (r as { speak?: unknown }).speak === "string"
      ? (r as { speak: string }).speak.trim() : "";
    if (speak) deps.onToolSpeak?.(speak);
    if (speak && deps.wasGated?.()) {
      return out({ ...(r as object), speak,
        instruction: "The user has not heard you this turn. Say the speak text once as written, then stop. Do not stay silent and do not paraphrase." });
    }
    return out(r);
  } catch (e) {
    if (e instanceof ApiError) return out({ ok: false, reason: e.detail });
    return out({ ok: false, reason: "cannot reach the local backend" });
  } finally {
    deps.onToolEnd?.(name);
  }
}

export function makeClientTools(deps: ToolDeps) {
  const { api, utter } = deps;
  return {
    submit_mandate_fields: (p: Record<string, unknown>) =>
      guarded(deps, "submit_mandate_fields", async () => {
        const u = utter.latest();
        if (!u) return NO_UTTERANCE;
        const round = Number(p?.round) === 1 ? 1 : 2;
        const r = await api.post("/mandate/fields", { utterance: u.text, round });
        deps.setDigest(r.recap?.digest ?? null);
        return { ok: true, speak: r.speak, stage: r.stage, missing_required: r.missing_required, ready: !!r.recap?.ready };
      }),

    confirm_mandate: () =>
      guarded(deps, "confirm_mandate", async () => {
        const u = utter.latest({ unusedOnly: true });
        if (!u) return NO_UTTERANCE;
        // first freeze needs the recap digest; a running session (another round / a settings change) may send "" for filter-only changes
        const digest = deps.getDigest() ?? "";
        const r = await api.post("/mandate/confirm", { utterance: u.text, recap_digest: digest });
        utter.markUsed(u);
        if (r.amended !== undefined) return { ok: true, speak: r.speak, amended: r.amended, kind: r.kind };
        return { ok: true, note: "mandate set; further progress will reach the user as system updates" };
      }),

    respond_to_card: (raw: Record<string, unknown>) =>
      guarded(deps, "respond_to_card", async () => {
        const p = (raw ?? {}) as { action?: string; code?: unknown; amount_usdc?: number };
        const action = p.action ?? "";
        if (!["approve", "reject", "modify"].includes(action)) return { ok: false, reason: "action must be approve / reject / modify" };
        const rc = typeof p.code === "number" ? String(p.code).padStart(2, "0") : typeof p.code === "string" ? p.code.trim() : "";
        const code = /^\d{2}$/.test(rc) ? rc : undefined;
        const u = utter.latest({ unusedOnly: action !== "reject" });
        if (!u && action !== "reject") return NO_UTTERANCE;
        const r = await api.post("/voice/card-respond", { action, code, utterance: u?.text, amount_usdc: p.amount_usdc });
        if (r.ok && action !== "reject") utter.markUsed(u);
        return r;
      }),

    respond_to_topup: (raw: Record<string, unknown>) =>
      guarded(deps, "respond_to_topup", async () => {
        const p = (raw ?? {}) as { action?: string; amount_usdc?: number };
        const action = p?.action === "approve" ? "approve" : "reject";
        const u = utter.latest({ unusedOnly: action === "approve" });
        if (!u && action === "approve") return NO_UTTERANCE;
        const r = await api.post("/research/topup", { action, amount_usdc: p?.amount_usdc, utterance: u?.text, channel: "voice" });
        if (r.ok && action === "approve") utter.markUsed(u);
        return r;
      }),

    place_orders: (raw: Record<string, unknown>) =>
      guarded(deps, "place_orders", async () => {
        const orders = deskOrders(raw);
        const u = utter.latest({ unusedOnly: true });
        if (!u) return NO_UTTERANCE;
        // the backend extracts coins and amounts from the user's own words; send agent numbers only when well-formed
        const r = await api.post("/desk/order", { orders: orders?.length ? orders : undefined, utterance: u.text, channel: "voice" });
        if (r.ok) utter.markUsed(u);
        return r;
      }),

    suggest_stop: (raw: Record<string, unknown>) =>
      guarded(deps, "suggest_stop", async () => {
        const n = (raw ?? {}).coin_number;
        const u = utter.latest();
        return api.post("/desk/stop", { coin_number: typeof n === "number" && n > 0 ? Math.round(n) : undefined, utterance: u?.text });
      }),

    cancel_settings_change: () =>
      guarded(deps, "cancel_settings_change", async () => {
        const r = (await api.post("/mandate/amend/cancel", {})) as { ok?: boolean; reason?: string };
        return r;   // the backend announces the cancel
      }),

    skip_coins: (raw: Record<string, unknown>) =>
      guarded(deps, "skip_coins", async () => {
        const nums = intList((raw ?? {}).coin_numbers);
        const u = utter.latest();
        const r = await api.post("/desk/skip", { coin_numbers: nums.length ? nums : undefined, utterance: u?.text, channel: "voice" });
        return r;
      }),

    simulate_price_move: (raw: Record<string, unknown>) =>
      guarded(deps, "simulate_price_move", async () => {
        const kind = (raw ?? {}).kind === "stop_loss" ? "stop_loss" : "take_profit";
        const u = utter.latest({ unusedOnly: true });
        if (!u) return NO_UTTERANCE;
        const r = await api.post("/test/price-move", { kind, utterance: u.text, channel: "voice" });
        if (r.ok) utter.markUsed(u);
        return r;
      }),

    get_status: () => guarded(deps, "get_status", () => api.get("/agent/status")),

    emergency_exit: () =>
      guarded(deps, "emergency_exit", async () => {
        const u = utter.latest({ unusedOnly: true });
        if (!u) return NO_UTTERANCE;
        const r = await api.post("/emergency-exit", { utterance: u.text, channel: "voice" });
        utter.markUsed(u);
        // duplex: the exit sells in the background ({status: "selling", positions}); fills arrive as system updates
        if (Array.isArray(r.results)) return { ok: true, sold: r.results.length };
        return r;
      }),

    stop_research: () =>
      guarded(deps, "stop_research", async () => {
        // safe direction (nothing is bought), so no confirmation words are required; the words still go along for the echo check
        const u = utter.latest();
        return api.post("/research/stop", { utterance: u?.text, channel: "voice" });
      }),

    manage_position: () =>
      guarded(deps, "manage_position", async () => {
        const u = utter.latest({ unusedOnly: true });
        if (!u) return NO_UTTERANCE;
        const r = await api.post("/positions/action", { utterance: u.text, channel: "voice" });
        if (r.ok) utter.markUsed(u);
        return r;
      }),

    skip_turn: () =>
      guarded(deps, "skip_turn", async () => ({ ok: true, silent: true, instruction: "Say nothing." })),
  };
}

export type ClientTools = ReturnType<typeof makeClientTools>;

function intList(v: unknown): number[] {
  const xs = Array.isArray(v) ? v : v == null ? [] : [v];
  return xs.map((x) => Number(x)).filter((x) => Number.isInteger(x) && x > 0 && x < 200);
}

/** Agent arguments -> [{coin_number, amount_usdc}]; [] = let the backend read it all from the words; null = malformed. */
export function deskOrders(raw: Record<string, unknown> | null | undefined): { coin_number: number; amount_usdc: number }[] | null {
  const p = raw ?? {};
  const nums = intList(p.coin_numbers);
  if (!nums.length) return [];
  const each = Number(p.amount_usdc_each);
  const list = Array.isArray(p.amounts_usdc) ? (p.amounts_usdc as unknown[]).map(Number) : [];
  if (list.length) {
    if (list.length !== nums.length || list.some((x) => !(x > 0))) return null;
    return nums.map((n, i) => ({ coin_number: n, amount_usdc: list[i] }));
  }
  if (each > 0) return nums.map((n) => ({ coin_number: n, amount_usdc: each }));
  return []; // coins without amounts: let the backend read the amount from the user's words
}
