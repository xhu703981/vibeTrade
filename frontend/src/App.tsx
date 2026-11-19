import { ConversationProvider, useConversationClientTool, useConversationControls, useConversationInput, useConversationMode, useConversationStatus } from "@elevenlabs/react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ApiError, makeApi, type Api } from "./api";
import { CandidatesPanel } from "./panels/CandidatesPanel";
import { CardsPanel } from "./panels/CardsPanel";
import { FilterTable } from "./panels/FilterTable";
import { GuardAlert, latestAlert } from "./panels/GuardAlert";
import { LiveLog } from "./panels/LiveLog";
import { PositionsPanel, pnl } from "./panels/PositionsPanel";
import { usdF, usdSigned } from "./panels/format";
import { SelectionDesk, failedCoins, type DeskOrder } from "./panels/SelectionDesk";
import { TopupPanel } from "./panels/TopupPanel";
import type { BackendEvent, Card } from "./types";
import { useBackend, useNow } from "./useBackend";
import { EMPTY_MEMORY, availableSheets, closeSheet, deskOffers, nextSheet, openSheet, pendingCards, pillRow, type SheetMemory } from "./ui/context";
import { Drawer, type DrawerTab } from "./ui/Drawer";
import { MicButton, VoiceLine, levelOf, type VoiceState } from "./ui/VoiceLine";
import { Sheet } from "./ui/Sheet";
import { CoinSheet, CoinTitle, GuardSheet, LimitsSheet, OrderSheet, PicksSheet, PositionSheet, TopupSheet, listedPicks, picksSource } from "./ui/sheets";
import { BroadcastQueue, UserActivity } from "./voice/broadcast";
import { ENGAGED_NOTE, Engagement, IDLE_NOTE, type Mode } from "./voice/engagement";
import { SIDE_TALK_NOTE, ShadowStream, type Hearing, type ShadowResult } from "./voice/shadow";
import { SpeechGate, flushConversationOutput } from "./voice/speechGate";
import { routeText } from "./voice/textRouter";
import { makeClientTools } from "./voice/tools";
import { UtteranceStore } from "./voice/utterances";
import "./styles.css";
import "./ui/voice.css";

import type { Line } from "./ui/context";

export default function App() {
  const api = useMemo(() => makeApi(), []);
  const utter = useMemo(() => new UtteranceStore(), []);
  const engagement = useMemo(() => new Engagement(), []);
  const [engageMode, setEngageMode] = useState<Mode>("idle");
  const syncMode = useCallback(() => setEngageMode(engagement.mode), [engagement]);
  const [lines, setLines] = useState<Line[]>([]);
  const addLine = useCallback((l: Line) => setLines((xs) => [...xs.slice(-30), l]), []);
  const activity = useMemo(() => new UserActivity(), []);
  // shadow listener: her live words go to the backend for staging / free prework only (nothing commits on them)
  const [hearing, setHearing] = useState<Hearing>(null);
  const sideTalk = useRef<() => void>(() => {});
  const heardAgent = useRef<(t: string) => void>(() => {});
  const agentReplied = useRef<() => void>(() => {});
  const displayAgent = useRef<(t: string) => boolean>(() => true);
  const armGate = useRef<(force?: boolean) => void>(() => {});
  const onBargeIn = useRef<() => void>(() => {});
  const muteIdle = useRef<() => void>(() => {});
  const unmuteIdle = useRef<() => void>(() => {});
  const shadow = useMemo(() => new ShadowStream(async (b) => (await api.post("/voice/partial", { ...b, mode: engagement.mode, answering: engagement.answeredQuestion, agent_said: engagement.lastAgentLine || undefined })) as ShadowResult, (r, final) => {
    if (r.stale) return;
    setHearing(r.hearing ?? null);
    if (final && r.gate_speech) armGate.current(true);
    if (final && (r.side_talk || r.addressed === false) && !engagement.answeredQuestion) {
      engagement.markSideTalk();
      setEngageMode(engagement.mode);
      muteIdle.current();
      sideTalk.current();
    }
  }), [api, engagement]);
  return (
    // Same signed URL + ConversationProvider in Realtime and Custom mode; the voice mode is an agent setting.
    <ConversationProvider
      onVadScore={({ vadScore }) => activity.vad(vadScore)}
      onInterruption={() => { activity.vad(1); onBargeIn.current(); }}
      onIncomingEvent={(ev: any) => {
        if (ev?.type === "tentative_user_transcript") shadow.partial(ev.tentative_user_transcription_event?.user_transcript ?? "");
      }}
      onMessage={(m) => {
        if (m.role === "user") {
          activity.finalTranscript();
          // final speech-recognition result = the user's raw words (system updates are filtered out by the UtteranceStore)
          const d = engagement.onUserText(m.message);
          setEngageMode(engagement.mode);
          if (utter.record(m.message, "voice", { ignored: !d.addressed })) {
            addLine({ who: "user", text: m.message, at: Date.now() });
            if (d.addressed) {
              unmuteIdle.current();
              armGate.current();
              shadow.final(m.message);
            } else {
              muteIdle.current();
              sideTalk.current();
            }
          }
        } else if ((agentReplied.current(), displayAgent.current(m.message))) {
          addLine({ who: "agent", text: m.message, at: Date.now() });
          heardAgent.current(m.message);
          engagement.agentLine(m.message);
          setEngageMode(engagement.mode);
          unmuteIdle.current();
        }
      }}
    >
      <Meeting api={api} utter={utter} lines={lines} addLine={addLine} activity={activity} shadow={shadow}
               hearing={hearing} clearHearing={() => setHearing(null)} sideTalk={sideTalk} heardAgent={heardAgent} agentReplied={agentReplied}
               displayAgent={displayAgent} armGate={armGate} onBargeIn={onBargeIn}
               engagement={engagement} engageMode={engageMode} syncMode={syncMode}
               muteIdle={muteIdle} unmuteIdle={unmuteIdle} />
    </ConversationProvider>
  );
}

function Meeting(props: { api: Api; utter: UtteranceStore; lines: Line[]; addLine: (l: Line) => void; activity: UserActivity;
                  shadow: ShadowStream; hearing: Hearing; clearHearing: () => void; sideTalk: { current: () => void };
                  heardAgent: { current: (t: string) => void }; agentReplied: { current: () => void }; displayAgent: { current: (t: string) => boolean };
                  armGate: { current: (force?: boolean) => void }; onBargeIn: { current: () => void };
                  engagement: Engagement; engageMode: Mode; syncMode: () => void;
                  muteIdle: { current: () => void }; unmuteIdle: { current: () => void } }) {
  const { api, utter, addLine, activity } = props;
  const controls = useConversationControls();
  const { status } = useConversationStatus();
  const { isSpeaking } = useConversationMode();
  const connected = status === "connected";
  const [textOnly, setTextOnly] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(0);
  const digest = useRef<string | null>(null);
  const toolsInFlight = useRef(0);
  const live = useRef({ connected, isSpeaking });
  live.current = { connected, isSpeaking };
  const stateRef = useRef<{ mandate_session?: { frozen?: boolean; amending?: boolean } } | null>(null);
  props.sideTalk.current = () => {
    if (live.current.connected) controls.sendContextualUpdate(props.engagement.mode === "idle" ? IDLE_NOTE : SIDE_TALK_NOTE);
  };
  const convRef = useRef<unknown>(null);
  const gate = useMemo(
    () => new SpeechGate((v) => { if (!live.current.connected) return; try { controls.setVolume({ volume: v }); } catch { /* no active session */ } }, () => flushConversationOutput(convRef.current)),
    [controls],
  );
  props.displayAgent.current = (t) => gate.displayAgent(t);
  // IDLE: the agent's reply to words that were not for it stays inaudible (and off the transcript).
  // gate.setVolume only touches the SDK while a session is connected.
  props.muteIdle.current = () => gate.muteIdle();
  props.unmuteIdle.current = () => gate.unmuteIdle();
  // Always arm on the user's turn, including after the mandate is frozen (status questions,
  // later amends). Skipping arm when frozen left pre-tool guesses on screen and in the speakers.
  props.armGate.current = (_force) => { gate.arm(); };
  props.onBargeIn.current = () => gate.disarm();

  // backend events -> agent broadcasts (queued while the agent speaks / a tool runs / the user is talking)
  const queue = useMemo(
    () => new BroadcastQueue(
      (t) => {
        // reading an update to her is the agent addressing her: ENGAGED and audible
        if (props.engagement.engage("update")) props.syncMode();
        gate.unmuteIdle();
        controls.sendUserMessage(t);
        addLine({ who: "system", text: t, at: Date.now() });
      },
      () => live.current.connected && !live.current.isSpeaking && toolsInFlight.current === 0,
      () => activity.talking(),
    ),
    [controls, addLine, activity, gate, props.engagement, props.syncMode],
  );
  props.heardAgent.current = (t) => queue.remember(t, false);
  props.agentReplied.current = () => queue.agentReplied();
  useEffect(() => { if (!isSpeaking) queue.flush(); else gate.noteSpeaking(); }, [isSpeaking, queue, gate]);
  useEffect(() => { const t = setInterval(() => queue.flush(), 500); return () => clearInterval(t); }, [queue]);
  useEffect(() => {
    const t = setInterval(() => {
      // The quiet timeout starts after the agent stops talking, the user stops talking and tools finish.
      // A confirmation card waiting for her answer also keeps the conversation open (cards expire on their own).
      if (live.current.isSpeaking || activity.talking() || toolsInFlight.current > 0 || props.engagement.pendingCodes.length) props.engagement.hold();
      const flipped = props.engagement.tick();
      if (flipped) {
        props.syncMode();
        gate.muteIdle();
        if (live.current.connected) controls.sendContextualUpdate(IDLE_NOTE);
      }
    }, 1000);
    return () => clearInterval(t);
  }, [props.engagement, props.syncMode, controls, activity, gate]);
  useEffect(() => { if (!connected) { queue.clear(); gate.disarm(); } }, [connected, queue, gate]);

  const onLive = useCallback((ev: BackendEvent) => {
    if (ev.loud || ev.type === "position_alert") {
      const flipped = props.engagement.engage("alert");
      props.syncMode();
      props.unmuteIdle.current();
      if (live.current.connected && flipped) controls.sendContextualUpdate(ENGAGED_NOTE);
    }
    if (live.current.connected) queue.push(ev);
    else if (ev.speak && ev.type !== "research_payment") addLine({ who: "system", text: ev.speak, at: Date.now() });
  }, [queue, addLine, props.engagement, props.syncMode, props.unmuteIdle, controls]);
  const { events, state, wsUp, refresh } = useBackend(api, onLive);
  stateRef.current = state;

  // client tools (structured args from the agent, raw words attached from the UtteranceStore)
  const tools = useMemo(() => makeClientTools({
    api, utter,
    getDigest: () => digest.current ?? state?.mandate_session.recap?.digest ?? null,
    setDigest: (d) => { digest.current = d; },
    onToolStart: () => { queue.agentReplied(); toolsInFlight.current++; props.clearHearing(); gate.onToolStart(); },
    // After a tool returns the agent answers its result; a system update sent at the same moment gets merged
    // into that answer and read twice, so updates wait for that line (skip_turn has none).
    onToolEnd: (name) => { if (name !== "skip_turn") queue.expectReply(); toolsInFlight.current = Math.max(0, toolsInFlight.current - 1); gate.release(); refresh(); setTimeout(() => queue.flush(), 300); },
    onToolSpeak: (speak) => queue.remember(speak, true),
    wasGated: () => gate.armed,
    isAddressed: () => props.engagement.mode === "engaged",
  }), [api, utter, state?.mandate_session.recap?.digest, refresh, queue, gate, props.engagement]);
  useConversationClientTool("submit_mandate_fields", tools.submit_mandate_fields);
  useConversationClientTool("confirm_mandate", tools.confirm_mandate);
  useConversationClientTool("respond_to_card", tools.respond_to_card);
  useConversationClientTool("respond_to_topup", tools.respond_to_topup);
  useConversationClientTool("get_status", tools.get_status);
  useConversationClientTool("emergency_exit", tools.emergency_exit);
  useConversationClientTool("place_orders", tools.place_orders);
  useConversationClientTool("skip_coins", tools.skip_coins);
  useConversationClientTool("suggest_stop", tools.suggest_stop);
  useConversationClientTool("cancel_settings_change", tools.cancel_settings_change);
  useConversationClientTool("simulate_price_move", tools.simulate_price_move);
  useConversationClientTool("stop_research", tools.stop_research);
  useConversationClientTool("manage_position", tools.manage_position);
  useConversationClientTool("skip_turn", tools.skip_turn);

  const start = async (text: boolean) => {
    setErr(null);
    try {
      if (!text) await navigator.mediaDevices.getUserMedia({ audio: true });
      // A new call loads the saved default settings and drops leftover frozen state from a previous run.
      const prepared = await api.post("/mandate/prepare") as { recap?: { digest?: string }; greeting?: string };
      if (prepared?.recap?.digest) digest.current = prepared.recap.digest;
      refresh();
      const { signed_url } = await api.get("/voice/signed-url");
      setTextOnly(text);
      props.engagement.engage("start");
      props.syncMode();
      gate.unmuteIdle();
      controls.startSession({ signedUrl: signed_url, onConversationCreated: (c) => { convRef.current = c; },
                               dynamicVariables: { greeting: prepared?.greeting || "Hi, I'm Vibe-Trader. Want to change anything before we look at coins?" },
                               ...(text ? { textOnly: true, overrides: { conversation: { textOnly: true } } } : {}) });
    } catch (e) {
      setErr(e instanceof ApiError ? `Voice did not connect: ${e.detail}` : `Voice did not connect: ${(e as Error).message}`);
    }
  };

  const act = async (fn: () => Promise<any>) => {
    setErr(null);
    try { await fn(); } catch (e) { setErr(e instanceof ApiError ? e.detail : String(e)); }
    refresh();
  };

  const [text, setText] = useState("");
  const sendText = async () => {
    const t = text.trim();
    if (!t) return;
    setText("");
    const d = props.engagement.onUserText(t);
    props.syncMode();
    addLine({ who: "user", text: t, at: Date.now() });
    if (connected) {
      utter.record(t, "typed", { ignored: !d.addressed });
      if (d.addressed) {
        props.unmuteIdle.current();
        props.armGate.current();
        props.shadow.final(t);
        queue.expectReply();
        controls.sendUserMessage(t);
        controls.sendContextualUpdate(ENGAGED_NOTE);
      } else {
        props.muteIdle.current();
        if (live.current.connected) controls.sendContextualUpdate(IDLE_NOTE);
        setTimeout(() => props.unmuteIdle.current(), 2000);
      }
    } else {
      await act(async () => {
        const r = await routeText(t, state, api, digest.current);
        addLine({ who: "system", text: r.reply, at: Date.now() });
      });
    }
  };

  const ms = state?.mandate_session;
  const desk = state?.desk;
  const phase = state?.phase ?? "";
  const showDesk = !!desk && (desk.offers.length > 0 || (desk.majors && desk.majors.length > 0) || ((state?.round ?? 0) > 0 && (phase === "selecting" || phase === "idle" || phase === "monitoring")));
  const liveMarket = showDesk || (state?.engine?.positions?.length ?? 0) > 0;
  useEffect(() => {
    if (!liveMarket) return;
    // never stack desk refreshes: a slow one would otherwise pile up and starve the agent's tool calls
    let busy = false;
    const tick = () => {
      if (busy) return;
      busy = true;
      api.post("/desk/refresh").catch(() => {}).finally(() => { busy = false; refresh(); });
    };
    tick();
    const t = setInterval(tick, 8_000);
    return () => clearInterval(t);
  }, [liveMarket, api]); // eslint-disable-line react-hooks/exhaustive-deps
  const failed = failedCoins(state?.candidates ?? [], state?.research ?? []);
  const washMax = ms?.mandate?.body?.filters?.max_volume_to_liquidity ?? ms?.draft?.filters?.max_volume_to_liquidity;
  const say = (text: string) => { addLine({ who: "system", text, at: Date.now() }); if (connected) controls.sendContextualUpdate(`On-screen action result: ${text}`); };
  const cards: Card[] = state?.engine?.cards ?? [];
  props.engagement.pendingCodes = cards.filter((c) => c.status === "pending" && c.code).map((c) => String(c.code));
  const buttons = !!state?.modes.ui_buttons;
  const alert = latestAlert(events, dismissed);

  // ---- voice screen state ----
  const { isMuted, setMuted } = useConversationInput();
  const [drawer, setDrawer] = useState(false);
  const [tab, setTab] = useState("settings");
  const [keyboard, setKeyboard] = useState(false);
  const [mem, setMem] = useState<SheetMemory>(EMPTY_MEMORY);
  const now = useNow(1000);
  const avail = useMemo(() => availableSheets(state, events, props.lines, now), [state, events, props.lines, now]);
  const availSig = avail.map((a) => `${a.key}~${a.trigger ?? ""}`).join(",");
  useEffect(() => {
    setMem((m) => { const n = nextSheet(m, avail); return n.current === m.current && n.seen.length === m.seen.length && n.manual === m.manual && n.fromTrigger === m.fromTrigger ? m : n; });
  }, [availSig]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (textOnly && connected) setKeyboard(true); }, [textOnly, connected]);
  const current = avail.find((a) => a.key === mem.current) ?? null;
  const wide = useWide();
  const dockRef = useRef<HTMLElement>(null);
  const [dockH, setDockH] = useState(96);
  useEffect(() => {
    const el = dockRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setDockH(Math.round(el.getBoundingClientRect().height)));
    ro.observe(el);
    for (const li of Array.from(el.children)) ro.observe(li);
    return () => ro.disconnect();
  }, []);
  const pills = pillRow(avail, mem.current);
  const positions = state?.engine?.positions ?? [];
  const openPos = positions.filter((p) => p.status === "open" && p.held_tokens > 0);
  const totalPnl = positions.reduce((a, p) => a + (pnl(p).value ?? 0), 0);
  // the limits in force (a pending change shows in its own sheet until it applies)
  const body = ms?.active?.body ?? ms?.mandate?.body ?? ms?.draft;
  const limits = body?.budget;

  const placeOrders = (orders: DeskOrder[]) => act(async () => {
    const r = await api.post("/desk/order", { orders, channel: "button" });
    // duplex: cards are built in the background; their codes arrive as the cards_ready update
    say(!r.ok ? `Not accepted: ${r.reason}`
        : r.status === "building_cards" ? `Building cards for coin ${(r.orders ?? []).map((x: any) => x.coin_number).join(", ")}…`
        : (r.cards ?? []).map((c: any) => `Card ${c.code}: coin ${c.coin_number}, ${c.amount_usdc} dollars`).join("; ") + ". Approve each by its code.");
  });
  const approve = (c: Card) => act(() => api.post(`/cards/${c.id}/respond`, { action: "approve", channel: "button", code: c.code }));
  const reject = (c: Card) => act(() => api.post(`/cards/${c.id}/respond`, { action: "reject", channel: "button" }));
  const applyAmend = (kind: "filters" | "guards" | "none") => act(async () => {
    const words = kind === "filters" ? "start research" : "confirm";
    const r = await api.post("/mandate/confirm", { utterance: words, recap_digest: kind === "guards" ? (digest.current ?? ms?.recap?.digest ?? "") : "" });
    if (r.speak) say(r.speak);
  });
  const cancelAmend = () => act(async () => { const r = await api.post("/mandate/amend/cancel"); if (r.speak) say(r.speak); });
  const topupApprove = (usd: number) => act(() => api.post("/research/topup", { action: "approve", amount_usdc: usd, channel: "button" }));
  const topupReject = () => act(() => api.post("/research/topup", { action: "reject", channel: "button" }));

  const working = !!state?.round_running || (state?.working_on?.length ?? 0) > 0;
  const voiceState: VoiceState = !connected ? (status === "connecting" ? "connecting" : "off")
    : isSpeaking ? "speaking"
    : working ? "working"
    : !textOnly && isMuted ? "muted"
    : props.engageMode === "idle" ? "idle" : "listening";
  const sample = () => {
    if (!connected) return null;
    if (isSpeaking) return controls.getOutputByteFrequencyData();
    return textOnly || isMuted ? null : controls.getInputByteFrequencyData();
  };
  const micLevel = () => (connected && !textOnly && !isMuted && !isSpeaking ? levelOf(controls.getInputByteFrequencyData()) : 0);
  const spoken = props.lines.filter((l) => l.who !== "system" || !connected);
  const lastLine = spoken[spoken.length - 1];
  // the agent's latest line waits for her answer: a question, or a "say confirm / approve" while a card, a limit change or a top-up waits
  const waitingOnHer = pendingCards(state).length > 0 || !!ms?.amending || !!state?.topup;
  const asking = connected && !!lastLine && lastLine.who !== "user"
    && (/\?\s*$/.test(lastLine.text) || (waitingOnHer && /\b(confirm|approve|cancel|reject)\b/i.test(lastLine.text)));
  const picks = showDesk && desk ? listedPicks(desk.offers) : [];
  const focusCoinNo = current?.kind === "coin" ? current.coin ?? null : null;

  const sheet = current && (() => {
    switch (current.kind) {
      case "picks": return null;   // the picks list is part of the main screen
      case "coin": {
        const o = deskOffers(state).find((x) => x.number === current.coin);
        return o ? { title: <CoinTitle offer={o} />, label: `Coin ${o.number} ${o.symbol}`, node: <CoinSheet api={api} offer={o} capUsdc={desk?.cap_usdc ?? 0} buttons={buttons}
                                                                  position={positions.find((p) => p.mint === o.mint && p.status === "open") ?? null}
                                                                  onOrder={(n, usd) => placeOrders([{ coin_number: n, amount_usdc: usd }])} /> } : null;
      }
      case "order": return { title: "Waiting for your OK", tone: "warn" as const, node: <OrderSheet cards={pendingCards(state)} buttons={buttons} onApprove={approve} onReject={reject} /> };
      case "position": return { title: openPos.length ? `${openPos.length} open position${openPos.length === 1 ? "" : "s"}` : "Positions", node: <PositionSheet positions={positions} /> };
      case "guard": {
        const e = events.find((x) => x.id === current.eventId);
        return e ? { title: "Guard check", tone: "bad" as const, node: <GuardSheet event={e} /> } : null;
      }
      case "limits": return { title: "Change your limits?", tone: "warn" as const, node: <LimitsSheet state={state!} onConfirm={() => applyAmend(ms?.amend_kind ?? "guards")} onCancel={cancelAmend} /> };
      case "topup": return state?.topup ? { title: "More research money?", tone: "warn" as const, node: <TopupSheet topup={state.topup} buttons={buttons} onApprove={topupApprove} onReject={topupReject} /> } : null;
    }
    return null;
  })();

  const tabs: DrawerTab[] = [
    { id: "settings", label: "Settings", badge: ms?.amending ? "1" : null, content: (
      <FilterTable draft={ms?.frozen ? ms.mandate?.body : ms?.draft} frozen={!!ms?.frozen} missing={ms?.missing_required ?? []} stage={ms?.stage}
                   amendAllowed={!!state?.amend?.allowed} blockedReason={state?.amend?.blocked_reason} amending={!!ms?.amending}
                   amendKind={ms?.amend_kind} amendChanges={ms?.amend_changes} version={state?.mandate_versions}
                   onCancelAmend={cancelAmend}
                   onApplyAmend={(kind) => applyAmend(kind)}
                   onEdit={(u) => act(async () => {
                     const r = await api.post("/mandate/fields", { utterance: u, round: 2 });
                     digest.current = r.recap?.digest ?? null;
                     addLine({ who: "system", text: r.speak, at: Date.now() });
                     if (connected) controls.sendContextualUpdate(`The user edited a setting in the on-screen table. Backend result: ${r.speak}`);
                   })} />
    ) },
    { id: "coins", label: "Coins", badge: desk?.offers.length || null, content: (
      showDesk && desk ? (
        <SelectionDesk api={api} desk={desk} failed={failed} buttonsEnabled={buttons} washMax={washMax}
                       onOrder={placeOrders}
                       onSkip={(numbers) => act(async () => { await api.post("/desk/skip", { coin_numbers: numbers, channel: "button" }); })}
                       onRefresh={() => act(() => api.post("/desk/refresh"))} />
      ) : <CandidatesPanel candidates={state?.candidates ?? []} research={state?.research ?? []} />
    ) },
    { id: "cards", label: "Cards", badge: props.engagement.pendingCodes.length || null, content: (
      <>
        {state?.topup && (
          <TopupPanel topup={state.topup} buttonsEnabled={buttons} onApprove={topupApprove} onReject={topupReject} />
        )}
        <CardsPanel cards={cards} buttonsEnabled={buttons} onApprove={approve} onReject={reject} />
      </>
    ) },
    { id: "positions", label: "Positions", badge: openPos.length || null, content: (
      <PositionsPanel positions={positions} canExit={!!state?.engine} testEnabled={!!state?.price_test_enabled}
                      onTest={(kind) => act(() => api.post("/test/price-move", { kind, channel: "button" }))}
                      onEmergency={() => { if (window.confirm("Sell all positions now?")) act(() => api.post("/emergency-exit", { channel: "button" })); }} />
    ) },
    { id: "activity", label: "Activity", content: (
      <>
        <GuardAlert event={alert} onDismiss={setDismissed} />
        <LiveLog events={events} state={state} />
      </>
    ) },
  ];
  const openLimits = () => { setTab("settings"); setDrawer(true); };
  const budget = limits?.total_usdc;
  const perTrade = limits?.per_trade_usdc;

  return (
    <div className={`vt ${sheet ? "has-sheet" : ""} ${picks.length ? "has-picks" : ""} ${keyboard ? "has-keys" : ""}`} style={{ ["--dock-h" as any]: `${dockH}px` }}>
      <header className="topbar">
        <span className="brand">Vibe<i className={`dot ${wsUp ? "on" : ""}`} title={wsUp ? "Connected" : "Reconnecting to the backend"} aria-label={wsUp ? "Connected" : "Reconnecting"} /></span>
        <span className="spacer" />
        {budget != null && (
          <button type="button" className="figures" onClick={openLimits} aria-label="Limits and today's result">
            <span className="fig"><span className="fig-l">Budget</span><span className="fig-v num">{usdF(budget)}</span></span>
            {perTrade != null && <span className="fig fig-opt"><span className="fig-l">Per trade</span><span className="fig-v num">{usdF(perTrade)}</span></span>}
            <span className="fig"><span className="fig-l">Today</span><span className={`fig-v num ${Math.round(totalPnl / 1e4) > 0 ? "up" : Math.round(totalPnl / 1e4) < 0 ? "down" : ""}`}>{usdSigned(totalPnl / 1e6)}</span></span>
          </button>
        )}
        <button className="icon" onClick={() => setDrawer(true)} aria-label="Details">
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden><path d="M4 7h16M4 12h16M4 17h10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>
        </button>
      </header>

      <main className="stage-v">
        <section className="talk" aria-label="Conversation">
          <VoiceLine state={voiceState} sample={sample}
                     aside={state?.round_running ? (
                       <button className="text-btn" onClick={() => act(async () => { const r = await api.post("/research/stop", { channel: "button" }); say(r.ok ? "Research round stopped. Nothing from it goes on the list." : r.reason); })}>Stop research</button>
                     ) : null} />
          <Conversation lines={spoken} asking={asking} connected={connected} configured={state?.voice_configured !== false}
                        pin={!!sheet && !wide} />
          <HearingStrip hearing={props.hearing ?? state?.hearing ?? null} talking={() => connected && !textOnly && activity.talking()} />
        </section>
        {picks.length > 0 && (
          <section className="picks-main" aria-label="Today's picks">
            <header className="picks-head">
              <h2>Today's picks</h2>
              <p className="picks-source">{picksSource(state!)}</p>
            </header>
            <PicksSheet api={api} state={state!} current={focusCoinNo} onOpenCoin={(n) => setMem((m) => openSheet(m, `coin:${n}`))} />
          </section>
        )}
      </main>

      {sheet && (
        <Sheet key={`${current!.key}-${wide ? "w" : "n"}`} sheetKey={current!.key} title={sheet.title} label={(sheet as any).label} tone={(sheet as any).tone} onClose={() => setMem((m) => closeSheet(m, avail))}>
          {sheet.node}
        </Sheet>
      )}

      <footer className="dock" ref={dockRef}>
        {(pills.length > 0 || positions.length > 0) && (
          <div className="replies" role="toolbar" aria-label="Quick replies">
            {positions.length > 0 && mem.current !== "position" && (
              <button className="reply" onClick={() => setMem((m) => openSheet(m, "position"))}>
                {openPos.length} open <span className={`num ${Math.round(totalPnl / 1e4) > 0 ? "up" : Math.round(totalPnl / 1e4) < 0 ? "down" : ""}`}>{usdSigned(totalPnl / 1e6)}</span>
              </button>
            )}
            {pills.filter((p) => p.kind !== "picks").map((p) => (
              <button key={p.key} className={`reply k-${p.kind}`} onClick={() => setMem((m) => openSheet(m, p.key))}>{p.label}</button>
            ))}
          </div>
        )}
        {err && <p className="toast" role="status">{err}</p>}
        <div className="controls-v">
          {keyboard && (
          <form className="keys" onSubmit={(e) => { e.preventDefault(); sendText(); }}>
            <input aria-label="Text input" autoFocus placeholder={connected ? "Message Vibe" : "Type to set things up, e.g. 10 dollars total, 2 per trade"}
                   value={text} onChange={(e) => { setText(e.target.value); if (connected) controls.sendUserActivity(); }} />
            <button type="submit" className="send" aria-label="Send" disabled={!text.trim()}>
              <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden><path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </button>
          </form>
        )}
          <button className={`icon kb ${keyboard ? "on" : ""}`} onClick={() => setKeyboard(!keyboard)} aria-label={keyboard ? "Hide keyboard" : "Type instead"} aria-pressed={keyboard}>
            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden><rect x="3" y="6" width="18" height="12" rx="2.5" stroke="currentColor" strokeWidth="1.7" fill="none" /><path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" /></svg>
          </button>
          {!connected ? (
            <>
              <MicButton level={() => 0} muted={false} connected={false} disabled={!state?.voice_configured || status === "connecting"}
                         onClick={() => start(false)} label="Start voice" />
              <button className="text-btn end" disabled={!state?.voice_configured || status === "connecting"} onClick={() => start(true)}>Text chat</button>
            </>
          ) : (
            <>
              {textOnly
                ? <MicButton level={() => 0} muted={false} connected={false} disabled onClick={() => {}} label="Voice is off in text chat" />
                : <MicButton level={micLevel} muted={isMuted} connected onClick={() => setMuted(!isMuted)} label={isMuted ? "Unmute" : "Mute"} />}
              <button className="text-btn end" onClick={() => controls.endSession()} aria-label="End call">End</button>
            </>
          )}
        </div>
      </footer>

      <Drawer open={drawer} onClose={() => setDrawer(false)} tabs={tabs} tab={tab} onTab={setTab}
              footer={<>
                <span className="soft small">{state?.modes.research === "live" ? "Live research: research costs real money." : "Practice mode: research replays recorded data."} Trades run on a local test chain with no real money.</span>
                <span className="soft small">Charts: <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts™</a>, copyright 2025 TradingView, Inc.</span>
                <span className="spacer" />
                <button className="text-btn" onClick={() => act(() => api.post("/mandate/reset"))} title="Start over with fresh settings">Reset</button>
              </>} />
    </div>
  );
}

/** Index of the line kept on screen while a phone sheet is open: Vibe's latest line (often the question waiting for
 *  her answer), or the latest line when Vibe has not spoken yet. */
export function pinnedIndex(lines: Line[]): number {
  for (let i = lines.length - 1; i >= 0; i--) if (lines[i].who === "agent") return i;
  return lines.length - 1;
}

/** Room for the pinned line: its rendered height, at most three lines (line height from the tokens). Taller lines scroll. */
export function pinnedHeight(lineHeightPx: number, renderedPx: number): number {
  return Math.ceil(lineHeightPx > 0 ? Math.min(renderedPx, lineHeightPx * 3) : renderedPx);
}

/** The conversation: left-aligned, speaker in small slate, the newest line largest. Older lines scroll up.
 *  pin (phone, sheet open): only Vibe's latest line stays, in full up to three lines; the sheet sits below it
 *  (--sheet-top on .vt, measured from the rendered line). */
export function Conversation(props: { lines: Line[]; asking: boolean; connected: boolean; configured: boolean; pin?: boolean }) {
  const box = useRef<HTMLOListElement>(null);
  const n = props.lines.length;
  const pin = !!props.pin && n > 0;
  const keep = pinnedIndex(props.lines);
  useLayoutEffect(() => {
    const el = box.current;
    const vt = (el?.closest(".vt") ?? null) as HTMLElement | null;
    if (!vt) return;
    if (!pin || !el) {
      vt.style.removeProperty("--conv-h");
      vt.style.removeProperty("--sheet-top");
      return;
    }
    const stage = el.closest(".stage-v") as HTMLElement | null;
    if (stage) stage.scrollTop = 0;               // the line sits at the top of the stage while the sheet is open
    const measure = () => {
      const li = el.querySelector("li.pinned") as HTMLElement | null;
      const text = li?.querySelector(".line-text") as HTMLElement | null;
      if (!li || !text) return;
      const h = pinnedHeight(parseFloat(getComputedStyle(text).lineHeight), li.offsetHeight);
      const top = el.getBoundingClientRect().top + (stage?.scrollTop ?? 0);
      vt.style.setProperty("--conv-h", `${h}px`);
      vt.style.setProperty("--sheet-top", `${Math.ceil(top + h)}px`);
    };
    measure();
    window.addEventListener("resize", measure);
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    const li = el.querySelector("li.pinned");
    if (ro && li) ro.observe(li);
    return () => { window.removeEventListener("resize", measure); ro?.disconnect(); };
  }, [pin, keep, n, props.lines[keep]?.text]);
  useEffect(() => {
    // show the newest line from its first word; if it is taller than the box, its top stays in view.
    // Re-run when the box or a line changes size (a sheet opening or closing hides or shows older lines).
    const el = box.current;
    if (!el) return;
    const toLatest = () => {
      const last = (pin ? el.querySelector("li.pinned") : el.lastElementChild) as HTMLElement | null;
      el.scrollTop = last && (pin || last.offsetHeight > el.clientHeight) ? last.offsetTop - el.offsetTop : el.scrollHeight;
    };
    toLatest();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(toLatest);
    ro.observe(el);
    for (const li of Array.from(el.children)) ro.observe(li);
    return () => ro.disconnect();
  }, [n, props.lines[n - 1]?.text, pin]);
  if (!n) {
    return (
      <p className="conv-empty">
        {props.configured ? "Start voice, or open text chat, to set your limits and look for coins." : "Voice isn't set up on this machine. Use text chat."}
      </p>
    );
  }
  return (
    <ol className={`transcript conv ${pin ? "pin" : ""}`} ref={box} aria-label="Transcript" aria-live="polite">
      {props.lines.map((l, i) => {
        const latest = i === n - 1;
        return (
          <li key={`${l.at}-${i}`} className={`${l.who} ${latest ? "latest" : "older"} ${latest && props.asking ? "asking" : ""} ${pin && i === keep ? "pinned" : ""}`}>
            <span className="who">{l.who === "user" ? "You" : "Vibe"}</span>
            <span className="line-text">{l.text}</span>
          </li>
        );
      })}
    </ol>
  );
}

/** Wide layout (side panel) vs phone layout (bottom sheet); the sheet remounts on a switch so its chart re-measures. */
function useWide(): boolean {
  const q = "(min-width: 900px)";
  const [wide, setWide] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.(q).matches);
  useEffect(() => {
    const m = window.matchMedia?.(q);
    if (!m) return;
    const on = () => setWide(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return wide;
}


/** Shadow listener: "Listening…" while she talks, "Hearing: <staged reading>" once the words say something we recognise.
 *  Display only; the reading disappears when a tool call commits (or discards) it, or after a few seconds. */
export function HearingStrip(props: { hearing: Hearing; talking: () => boolean; now?: () => number }) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 300); return () => clearInterval(t); }, []);
  const now = (props.now ?? Date.now)() / 1000;
  const h = props.hearing && now - props.hearing.at <= 8 ? props.hearing : null;
  const talking = props.talking();
  if (!h && !talking) return null;
  return (
    <div className="hearing" role="status" aria-label="Shadow listener">
      {talking && <span>Listening…</span>}
      {h && <span className="heard">Hearing: {h.label}…</span>}
    </div>
  );
}

/** Duplex: what is still running in the background while the conversation goes on. Stopping research is always allowed (safe). */
export function BackgroundWork(props: { items: string[]; roundRunning: boolean; onStop: () => void }) {
  if (!props.items.length && !props.roundRunning) return null;
  return (
    <div className="bgwork" role="status" aria-label="Background work">
      <span className="small">Working on {props.items.length ? props.items.join(", ") : "the research round"}</span>
      {props.roundRunning && <button className="text-btn" onClick={props.onStop}>Stop research</button>}
    </div>
  );
}
