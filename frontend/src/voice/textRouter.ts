// Direct text mode (no ElevenLabs): route what the user typed to the right endpoint for the current state.
// It goes through the same backend checks as voice (raw words + code); this only decides where to send it.
// English is the primary path; Chinese words remain as extra aliases.
import type { Api } from "../api";
import type { AppState } from "../types";

export type RouteResult = { target: string; reply: string };

const SELL_ALL = /\bsell\s+(it\s+)?all\b|\bsell\s+everything\b|\bemergency\s+exit\b|\bexit\s+all\b|\bliquidate\b|全部卖|全卖|清仓|卖光/i;
const REJECT = /\b(reject|cancel|decline|skip|no|nope|don'?t|do not)\b|拒绝|不要|算了|取消|不追加|不加/i;
const MODIFY = /\b(change|modify|make it|set it to)\b|改成|改为|换成/i;
const CONFIRM = /\bconfirm(ed)?\b|确认/i;
const SIMULATE = /\b(simulate|test|trigger|fake)\b[\w\s,-]{0,20}?\b(take[\s-]*profit|tp|stop[\s-]*loss|sl)\b/i;
const SKIP = /\b(skip|pass)\b|\b(don'?t|do not|no)\s+buy\b|跳过|不买/i;
const DESK = /\b(coins?|number|first|second|third|fourth|fifth|all of them|both)\b|\$\s*\d|\d\s*(dollars?|usdc?|bucks|cents?)\b|跳过|不买/i;
const STOP_RESEARCH = /\b(stop|cancel|abort|halt)\b[\w\s]{0,12}?\b(research|search|round|looking)\b|停止研究|别找了|停止搜索/i;
const STOP_Q = /\bwhere\b[\w\s']{0,20}\bstop\b|\bwhat\s+stop\b|\bstop\b[\w\s']{0,12}\b(should|would)\b/i;
const RISK = /\b(lose|losing|risk|risking)\b/i;
const YES = /^\s*(yes|yeah|yep|sure|ok(ay)?|do it|go ahead|please do|sounds good)\b/i;
const PRESET_OK = /^\s*(no|nope|looks good|look good|same|same as last time|go ahead|sure|yes|yeah|that'?s fine|all good)\b/i;
// words that make a typed line about trading; anything else while a session runs is side talk and is ignored
const TRADING = /\b(coins?|stop|sell|buy|approve|reject|lose|risk|yes|yeah|no|confirm|research|round|market|cap|budget|dollars?|cents?|percent|card|status|vibe|half|position|change|limit|trade|exit|skip|profit|loss|simulate|price|emergency|filters?|liquidity|holders|settings|go|start|look|ok(ay)?|sure|same)\b|\d|确认|卖|买|研究|跳过|清仓/i;
const NEW_ROUND = /\b(another|new|next)\s+(research\s+)?round\b|\bresearch\s+again\b|\bstart\s+(the\s+)?research\b|\bfind\s+(me\s+)?(new|other|more)\s+coins\b|再找|新一轮/i;

export async function routeText(text: string, st: AppState | null, api: Api, digest: string | null): Promise<RouteResult> {
  const t = text.trim();
  const ms = st?.mandate_session;
  if (ms?.frozen && !TRADING.test(t)) return { target: "ignored", reply: "" };   // side talk ("two lattes, please")
  if (SELL_ALL.test(t) && st?.engine) {
    const r = await api.post("/emergency-exit", { utterance: t, channel: "voice" });
    const n = Array.isArray(r.results) ? r.results.length : r.positions ?? 0;
    return { target: "emergency_exit", reply: r.ok === false ? `Not accepted: ${r.reason}` : `Emergency exit: selling ${n} positions` };
  }
  if (STOP_RESEARCH.test(t) && st?.round_running) {
    const r = await api.post("/research/stop", { utterance: t, channel: "voice" });
    return { target: "research_stop", reply: r.ok ? "Research round stopped; nothing from it goes on the desk." : `Not accepted: ${r.reason}` };
  }
  const sim = t.match(SIMULATE);
  if (sim && st?.engine) {
    const kind = /^(take|tp)/i.test(sim[2]) ? "take_profit" : "stop_loss";
    const r = await api.post("/test/price-move", { kind, utterance: t, channel: "voice" });
    return { target: "price_test", reply: r.ok ? "Test price move started; the listener sells as usual." : `Not accepted: ${r.reason}` };
  }
  if (STOP_Q.test(t) && ms?.frozen) {
    const r = await api.post("/desk/stop", { utterance: t });
    return { target: "stop_suggest", reply: r.speak ?? (r.ok ? "" : `Not accepted: ${r.reason}`) };
  }
  const offers = (st?.engine?.cards ?? []).filter((c) => c.status === "pending" && c.offer);
  if (offers.length === 1 && YES.test(t) && !REJECT.test(t.replace(/^\s*no\b/i, ""))) {
    const r = await api.post("/voice/card-respond", { action: "approve", utterance: t });
    return { target: "card", reply: r.ok ? `Card ${offers[0].code} ${r.status ?? ""}` : `Not accepted: ${r.reason ?? ""}` };
  }
  const deskOpen = (st?.desk?.offers ?? []).some((o) => o.status === "open" || o.status === "skipped");
  if (deskOpen && !ms?.amending && (DESK.test(t) || RISK.test(t)) && !NEW_ROUND.test(t)) {
    if (SKIP.test(t) && !/\$|\bdollars?\b|\bcents?\b|\busdc?\b/i.test(t)) {
      const r = await api.post("/desk/skip", { utterance: t, channel: "voice" });
      return { target: "desk_skip", reply: r.ok ? `Skipped coin ${(r.numbers ?? []).join(", ")}` : `Not accepted: ${r.reason}` };
    }
    const r = await api.post("/desk/order", { utterance: t, channel: "voice" });
    return {
      target: "desk_order",
      reply: !r.ok ? `Not accepted: ${r.reason}` : r.status === "building_cards" ? `Building cards for coin ${(r.orders ?? []).map((x: any) => x.coin_number).join(", ")}…` : (r.cards ?? []).map((c: any) => `Card ${c.code}: coin ${c.coin_number}, ${c.amount_usdc} dollars`).join("; ") + ". Approve each by its code.",
    };
  }
  if (st?.topup) {
    const reject = REJECT.test(t);
    const m = t.match(/(\d+(?:\.\d+)?)\s*(cents?|毛|角)?/i);
    const amount = m ? Number(m[1]) / (m[2] ? (/cent/i.test(m[2]) ? 100 : 10) : 1) : undefined;
    const r = await api.post("/research/topup", { action: reject ? "reject" : "approve", amount_usdc: amount, utterance: t, channel: "voice" });
    return { target: "topup", reply: r.ok ? (reject ? "No top-up" : `Added ${amount} dollars`) : `Not accepted: ${r.reason}` };
  }
  const pending = (st?.engine?.cards ?? []).filter((c) => c.status === "pending");
  if (pending.length && /\d{2}|\b(approve|reject|change)\b|拒绝|批准|改成/i.test(t)) {
    const code = pending.map((c) => c.code).find((c) => new RegExp(`(?<!\\d)${c}(?!\\d)`).test(t));
    const action = REJECT.test(t) ? "reject" : MODIFY.test(t) ? "modify" : "approve";
    const amt = action === "modify" ? Number((t.replace(code ?? "", "").match(/\d+(?:\.\d+)?/) ?? [])[0]) : undefined;
    const r = await api.post("/voice/card-respond", { action, code, utterance: t, amount_usdc: amt || undefined });
    return { target: "card", reply: r.ok ? `Card ${code ?? ""} ${r.status ?? ""}` : `Not accepted: ${r.reason ?? ""}` };
  }
  // running session: settings changes and new rounds (the mandate is never read-only between trades)
  if (ms?.frozen || ms?.amending) {
    if (ms.amending && (CONFIRM.test(t) || NEW_ROUND.test(t) || /^\s*(go|go ahead|do it|yes)\b/i.test(t))) {
      const r = await api.post("/mandate/confirm", { utterance: t, recap_digest: digest ?? ms.recap?.digest ?? "" });
      return { target: "amend_apply", reply: r.speak ?? "Applied" };
    }
    const r = await api.post("/mandate/fields", { utterance: t, round: 2 });
    return { target: "amend", reply: r.speak ?? "" };
  }
  if (ms && !ms.frozen) {
    digest = digest ?? ms.recap?.digest ?? null;
    if (ms.recap?.ready && (CONFIRM.test(t) || PRESET_OK.test(t)) && digest) {
      const r = await api.post("/mandate/confirm", { utterance: t, recap_digest: digest });
      return { target: "confirm", reply: r.speak ?? "Frozen" };
    }
    const round = ms.stage ? (ms.stage === "funds" ? 1 : 2) : (ms.missing_required ?? []).some((p) => p.startsWith("budget")) ? 1 : 2;
    const r = await api.post("/mandate/fields", { utterance: t, round });
    return { target: "mandate", reply: r.speak ?? "" };
  }
  return { target: "none", reply: "Nothing is waiting for an answer right now. You can ask \"what's the status?\" or say \"sell all\"." };
}
