"""FastAPI entry point: mandate (M1), confirmation cards / status / emergency exit (M5), voice + event push + orchestration (M6).

Single user, in-memory session; enough for a hackathon demo. Run: .venv/bin/uvicorn backend.app:app --port 8000
Every state-changing voice request carries `utterance`: the user's raw words sent separately by the frontend (ASR text or typed text), never an agent tool argument.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

# Logs go to redirected files (logs/backend.out / .err). On Windows those default to the ANSI code page (cp1252 / cp936),
# which can't encode emoji in token names: never let a log line crash a request or a background task.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

from backend.execute.listener import money_words
from backend.judge.jev import JEVClient
from backend.judge.llm import chat_json
from backend.events import EventBus
from backend.mandate.dialogue import CONFIRM_RE as CONFIRM_WORD_RE, LIMIT_PATHS, MandateFrozenError, MandateSession, is_confirmation
from backend.orchestrator import Orchestrator
from backend.voice.verify import echo_problem

@contextlib.asynccontextmanager
async def _lifespan(_app):
    yield
    with contextlib.suppress(Exception):
        await asyncio.wait_for(_app.state.orch.close(), 10)


app = FastAPI(title="Vibe Trading backend", lifespan=_lifespan)


def new_session() -> MandateSession:
    from backend.execute.price_source import sol_usd_price
    from backend.mandate.speculative import SpecCache
    return MandateSession(extractor=chat_json, judge=JEVClient(), sol_usd=sol_usd_price, spec=SpecCache())


app.state.session = new_session()
app.state.bus = EventBus()
app.state.orch = Orchestrator(app.state.bus)


def _shadow_ctx() -> dict:
    """What the shadow listener may look at: read-only facts about the session (never token names)."""
    s: MandateSession = app.state.session
    o: Orchestrator = app.state.orch
    eng = o.engine or app.state.engine
    desk = o._desk()
    return {"frozen": s.frozen is not None or s.amending, "round_running": o.round_running(),
            "amending": bool(s.amending),
            "amend_blocked": bool(s.frozen is not None and o.amend_block_reason()),
            "desk_open": [d["number"] for d in desk if d["status"] in ("open", "skipped")],
            "pending_codes": [c.code for c in (eng.cards.values() if eng else []) if c.status == "pending"]}


def _shadow_pools() -> dict[int, str]:
    return {d["number"]: d["pool"] for d in app.state.orch._desk() if d.get("pool")}


async def _warm_candles(pool: str) -> None:
    from backend.market.ohlcv import fetch_ohlcv
    await fetch_ohlcv(pool, "15m")      # free GeckoTerminal, cached; the desk chart then opens instantly


def _new_shadow():
    import os
    from backend.voice.shadow import ShadowListener
    return ShadowListener(_shadow_ctx, lambda: app.state.session, _warm_candles, _shadow_pools,
                          speculate_extract=os.environ.get("VIBE_SPECULATE_EXTRACT", "1") != "0")


app.state.shadow = _new_shadow()


def _shadow_done(utterance: str | None) -> None:
    app.state.shadow.committed(utterance)


def _recent_speak(n: int = 30) -> list[str]:
    return [e.speak for e in app.state.bus.history[-n:] if e.speak]


def _check_echo(utterance: str | None, target: str) -> None:
    """If the raw words echo a system broadcast, reject (409) and emit voice_rejected."""
    why = echo_problem(utterance, _recent_speak())
    if why:
        app.state.bus.emit("voice_rejected", f"I did not use that sentence ({why}). Please say it again yourself.", {"target": target, "why": why})
        raise HTTPException(409, why)


def _mandate_event(s: MandateSession) -> None:
    if s.frozen is not None:
        app.state.bus.emit("mandate_updated", "", {"frozen": True, "body": s.frozen.body.model_dump(mode="json"), "stage": "running"})
        return
    app.state.bus.emit("mandate_updated", "", {"draft": s.draft, "missing_required": s.missing_required(), "stage": s.stage(),
                                               "amending": s.amending, "recap": s.recap() if not s.missing_required() else None})


class FieldsIn(BaseModel):
    utterance: str = Field(min_length=1, max_length=2000)
    round: int = Field(default=1, ge=1, le=2)


class ConfirmIn(BaseModel):
    utterance: str = Field(min_length=1, max_length=500)
    recap_digest: str = ""
    start_pipeline: bool = True



@app.post("/mandate/fields")
async def mandate_fields(body: FieldsIn) -> dict:
    """Setup turns before the first freeze; afterwards the same tool amends the running session (filters / guards)."""
    s: MandateSession = app.state.session
    o: Orchestrator = app.state.orch
    _shadow_done(body.utterance)
    _check_echo(body.utterance, "mandate")
    opened = False
    if s.frozen is not None:
        why = o.amend_block_reason()
        if why:
            return {"ok": False, "blocked": True, "reason": why, "stage": "amend", "missing_required": [], "ready": False,
                    "outcomes": [], "speak": f"I can't change the settings right now: {why}."}
        s.open_amend()
        opened = True
    try:
        res = await s.submit(body.utterance, body.round)
    except MandateFrozenError as e:
        raise HTTPException(409, str(e))
    out = res.as_dict()
    if s.amending:
        kind = s.amend_kind()
        problems = any(x.status != "accepted" for x in res.outcomes)
        # judged on what actually changed in the draft (a rejected side request in the same sentence changes nothing)
        direction = s.amend_direction()
        if direction == "tighten":
            # lowering the user's own limits is the safe direction: effective now, short spoken acknowledgement
            voided = o.void_for_amend()
            parts = [f"{lbl} is now {_limit_words(lbl, new)}" for lbl, _old, new in s.limit_moves()]
            speak = "Done, " + " and ".join(parts) + ", effective now."
            m = s.apply_tightening(body.utterance)
            o.apply_now(m, speak)
            out.update({"applied": True, "amending": False, "mandate_id": m.mandate_id, "speak": speak, "ready": False})
            if voided:
                out["voided_cards"] = voided
            _mandate_event(s)
            return out
        if direction == "loosen":
            # raising a limit loosens a guardrail: ask out loud; only an explicit "confirm" applies it (/mandate/confirm)
            parts = [f"your {lbl} from {_limit_words(lbl, old)} to {_limit_words(lbl, new)}" for lbl, old, new in s.limit_moves()]
            q = ("That raises " + " and ".join(parts) + ". That loosens a safety limit, so say confirm to apply it, "
                 "or cancel to keep it as it is.")
            if getattr(s, "loosen_asked", None) != q:
                s.loosen_asked = q
                app.state.bus.emit("amend_confirm_needed", q, {"changes": s.amend_changes()}, loud=True)
            out["speak"] = q
            out["needs_spoken_confirm"] = True
        if kind == "none" and not res.proceed:
            if opened:
                s.cancel_amend()           # nothing changed: stay on the running mandate, nothing is voided
            out.update({"amending": s.amending, "ready": False})
            _mandate_event(s)
            return out
        if kind != "none":
            voided = o.void_for_amend()      # pending cards never execute under rules that are being changed
            if voided:
                out["voided_cards"] = voided
        if opened:
            app.state.bus.emit("amend_opened", "", {"kind": kind})
        out["amending"] = True
        out["recap"] = s.recap()
        out["ready"] = True
        if res.proceed and kind in ("filters", "none") and not problems:
            # light confirm: the user changed only hunt filters (or nothing) and asked for the new round in the same breath
            m = s.apply_amend(body.utterance)
            await o.new_round(m, kind)
            out.update({"applied": True, "amending": False, "mandate_id": m.mandate_id})
        _mandate_event(s)
        return out
    if not res.missing_required:  # all required fields present: return the code-generated recap and its digest, which the confirmation must carry.
        out["recap"] = s.recap()      # the full written recap is for the screen / audit; speak stays short and points at the screen
    out["ready"] = not res.missing_required
    _mandate_event(s)
    return out


def _limit_words(label: str, v) -> str:
    if v is None:
        return "no limit"
    if label == "max fee":
        return f"{v:g} SOL"
    return money_words(round(float(v) * 1e6))


@app.post("/mandate/amend/cancel")
async def mandate_amend_cancel() -> dict:
    s: MandateSession = app.state.session
    if not s.amending:
        return {"ok": False, "reason": "no settings change in progress"}
    s.cancel_amend()
    s.loosen_asked = None
    app.state.bus.emit("amend_cancelled", "OK, cancelled. Your limits stay as they were.", {})
    _mandate_event(s)
    return {"ok": True}


@app.get("/mandate/draft")
async def mandate_draft() -> dict:
    s: MandateSession = app.state.session
    if s.frozen:
        return {"frozen": True, "mandate": s.frozen.model_dump(mode="json")}
    return {"frozen": False, "draft": s.draft, "missing_required": s.missing_required(), "stage": s.stage(), "recap": s.recap()}


@app.post("/mandate/confirm")
async def mandate_confirm(body: ConfirmIn) -> dict:
    """First freeze, or (later) apply an amendment / start another research round."""
    s: MandateSession = app.state.session
    _check_echo(body.utterance, "mandate_confirm")
    if s.frozen is not None or s.amending:
        o: Orchestrator = app.state.orch
        why = o.amend_block_reason()
        if why:
            raise HTTPException(409, f"not now: {why}")
        if not s.amending:
            s.open_amend()
        kind = s.amend_kind()
        if s.amend_direction() == "loosen":
            if not is_confirmation(body.utterance or ""):
                q = "To raise a safety limit I need the word confirm. Say confirm to apply it, or cancel to keep it as it is."
                app.state.bus.emit("amend_confirm_needed", q, {"changes": s.amend_changes()}, loud=True)
                raise HTTPException(400, q)
            if all(p in LIMIT_PATHS for p in s.amend_changes()):
                # only the user's own limits changed: effective now after the spoken "confirm"; desk and round stay
                parts = [f"your {lbl} is now {_limit_words(lbl, new)}" for lbl, _old, new in s.limit_moves()]
                try:
                    m = s.apply_amend(body.utterance, body.recap_digest)
                except ValueError as e:
                    raise HTTPException(400, str(e))
                s.loosen_asked = None
                o.void_for_amend()
                speak = "Done, " + " and ".join(parts) + ", effective now."
                o.apply_now(m, speak)
                _mandate_event(s)
                return {"frozen": True, "amended": True, "kind": "limits", "mandate": m.model_dump(mode="json"),
                        "stopped_previous_round": False, "speak": speak}
        try:
            m = s.apply_amend(body.utterance, body.recap_digest)
        except ValueError as e:
            if kind == "none":
                s.cancel_amend()
            raise HTTPException(400, str(e))
        s.loosen_asked = None
        if kind != "none":
            o.void_for_amend()
        stopped = o.round_running()
        await o.new_round(m, kind)
        _mandate_event(s)
        return {"frozen": True, "amended": kind != "none", "kind": kind, "mandate": m.model_dump(mode="json"),
                "stopped_previous_round": stopped,
                "speak": (f"Settings updated, version {o.versions}. New research round started." if kind != "none"
                          else "New research round started with the same settings.")}
    try:
        m = s.confirm(body.utterance, body.recap_digest)
    except MandateFrozenError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.start_pipeline:
        app.state.orch.start(m)   # emits mandate_frozen; discovery starts in the background
    return {"frozen": True, "mandate": m.model_dump(mode="json"),
            "speak": f"Mandate frozen, ID {m.mandate_id[-6:]}."}


class StopIn(BaseModel):
    utterance: str | None = Field(default=None, max_length=500)
    channel: Literal["voice", "button"] = "voice"


@app.post("/research/stop")
async def research_stop(body: StopIn | None = None) -> dict:
    """Stop the running research round (safe direction: nothing is bought; no new research payment starts)."""
    body = body or StopIn()
    _shadow_done(body.utterance)
    if body.channel == "voice":
        _snippet_echo(body.utterance, "research_stop")
    o: Orchestrator = app.state.orch
    if not await o.stop_round("user"):
        return {"ok": False, "reason": "no research round is running"}
    return {"ok": True, "status": "stopped", "phase": o.current_phase()}


def _bind_override_log() -> None:
    def _note(detail: dict) -> None:
        s: MandateSession = app.state.session
        s.transcript.append({"position_override": detail})
    app.state.orch.override_hook = _note


_bind_override_log()


def _reset_session() -> None:
    """Drop leftover mandate / pipeline / engine state (used by Reset and by a new voice call)."""
    app.state.bus.new_session()
    app.state.shadow.close()
    app.state.shadow = _new_shadow()
    app.state.engine = None
    app.state.session = new_session()
    _bind_override_log()


@app.post("/mandate/reset")
async def mandate_reset() -> dict:
    """Dev helper: start a new empty mandate (also stops the background pipeline and clears the engine)."""
    await app.state.orch.close()
    app.state.orch.reset()
    _reset_session()
    _mandate_event(app.state.session)
    return {"ok": True}


@app.post("/mandate/prepare")
async def mandate_prepare() -> dict:
    """Start of a call: forget leftover frozen state and load the saved default settings (not frozen yet).

    The agent greets with the settings on screen and asks if the user wants to change any of them.
    'no' / 'looks good' / 'go ahead' is a confirm and starts research.
    """
    await app.state.orch.close()
    app.state.orch.reset()
    _reset_session()
    from backend.mandate.preset import greeting_text, load_preset
    s = app.state.session
    s.apply_preset(load_preset())
    recap = s.recap()
    _mandate_event(s)
    return {"ok": True, "preset": True, "draft": s.draft, "missing_required": s.missing_required(),
            "stage": s.stage(), "ready": bool(recap.get("ready")), "recap": recap,
            "greeting": greeting_text(s.draft),
            "speak": "The saved settings are on the screen. Do you want to change any of these settings?"}


# ---------------- M5: confirmation cards / status / emergency exit ----------------
# app.state.engine is injected by the start script (TradingEngine, local Surfpool fork only).

from typing import Literal  # noqa: E402

app.state.engine = None


class CardRespondIn(BaseModel):
    action: Literal["approve", "reject", "modify"]
    # voice channel: the frontend sends this turn's ASR text separately (not an agent tool argument)
    utterance: str | None = Field(default=None, max_length=500)
    amount_usdc: float | None = Field(default=None, gt=0)
    channel: Literal["voice", "button"] = "voice"
    code: str | None = Field(default=None, pattern=r"^\d{2}$")  # button channel: the code shown on screen
    # duplex voice: by default long work runs in the background and the outcome arrives as a system update; wait=True blocks (scripts / tests)
    wait: bool = False


def _engine():
    e = app.state.engine or app.state.orch.engine
    if e is None:
        raise HTTPException(409, "the trading engine is not running")
    return e


@app.post("/cards/{card_id}/respond")
async def card_respond(card_id: str, body: CardRespondIn) -> dict:
    e = _engine()
    if body.channel == "voice" and body.action != "reject":
        _check_echo(body.utterance, "card")
    if body.action == "modify" and body.amount_usdc is None:
        raise HTTPException(400, "changing the amount needs amount_usdc")
    return await e.respond(card_id, body.action, utterance=body.utterance, amount_usdc=body.amount_usdc,
                           channel=body.channel, code=body.code, background=not body.wait)


@app.get("/status")
async def status() -> dict:
    """M5-compatible: engine status (plus the M6 orchestrator state) when an engine exists; 409 otherwise."""
    out = _engine().status()
    out["orchestrator"] = app.state.orch.status()
    return out


class EmergencyIn(BaseModel):
    utterance: str | None = Field(default=None, max_length=500)
    channel: Literal["voice", "button"] = "button"
    # duplex voice: by default long work runs in the background and the outcome arrives as a system update; wait=True blocks (scripts / tests)
    wait: bool = False


@app.post("/emergency-exit")
async def emergency_exit(body: EmergencyIn | None = None) -> dict:
    body = body or EmergencyIn()
    _shadow_done(body.utterance)
    e = _engine()
    if body.channel == "voice":
        _check_echo(body.utterance, "emergency_exit")
        o = app.state.orch
        if o.engine is e:
            r = await o.emergency_exit(body.utterance, "voice", wait=body.wait)
            if not r["ok"]:
                raise HTTPException(400, r["reason"])
            return r
        from backend.voice.verify import verify_sell_all
        why = verify_sell_all(body.utterance)
        if why:
            raise HTTPException(400, why)
    o = app.state.orch
    if o.engine is e:
        return await o.emergency_exit(None, "button", wait=body.wait)
    return {"ok": True, "results": await e.emergency_exit()}


class DeskOrderItem(BaseModel):
    coin_number: int = Field(ge=1, le=200)
    amount_usdc: float = Field(gt=0)


class DeskOrderIn(BaseModel):
    orders: list[DeskOrderItem] | None = Field(default=None, max_length=30)
    utterance: str | None = Field(default=None, max_length=500)
    channel: Literal["voice", "button"] = "voice"
    # duplex voice: by default long work runs in the background and the outcome arrives as a system update; wait=True blocks (scripts / tests)
    wait: bool = False


class DeskSkipIn(BaseModel):
    coin_numbers: list[int] | None = Field(default=None, max_length=30)
    utterance: str | None = Field(default=None, max_length=500)
    channel: Literal["voice", "button"] = "voice"


def _snippet_echo(utterance: str | None, target: str) -> None:
    # marker / broadcast-snippet check only: a short phrase like "coin 1, 5 dollars" can legitimately appear in a broadcast
    why = echo_problem(utterance, [])
    if why:
        app.state.bus.emit("voice_rejected", f"I did not use that sentence ({why}). Please say it again yourself.", {"target": target, "why": why})
        raise HTTPException(409, why)


@app.post("/desk/order")
async def desk_order(body: DeskOrderIn) -> dict:
    """Pick any number of desk coins with an amount each -> one confirmation card per coin ("approve NN" still needed)."""
    s: MandateSession = app.state.session
    _shadow_done(body.utterance)
    if s.amending and s.amend_kind() != "none":
        return {"ok": False, "reason": "a settings change is in progress; confirm or cancel it first"}
    if body.channel == "voice":
        _snippet_echo(body.utterance, "desk")
    orders = {i.coin_number: i.amount_usdc for i in body.orders} if body.orders else None
    if body.orders and len(orders) != len(body.orders):
        return {"ok": False, "reason": "the same coin was listed twice"}
    return await app.state.orch.place_orders(orders, body.utterance, body.channel, wait=body.wait)


class StopAskIn(BaseModel):
    coin_number: int | None = Field(default=None, ge=1, le=999)
    utterance: str | None = Field(default=None, max_length=500)


@app.post("/desk/stop")
async def desk_stop(body: StopAskIn) -> dict:
    """"Where should my stop be?" -> the coin's volatility stop (ATR, 5-25%), one sentence. Read-only: nothing is bought."""
    _shadow_done(body.utterance)
    return await app.state.orch.suggest_stop(body.coin_number, body.utterance)


@app.post("/desk/skip")
async def desk_skip(body: DeskSkipIn) -> dict:
    return app.state.orch.skip_coins(body.coin_numbers, body.utterance, body.channel)


@app.post("/desk/refresh")
async def desk_refresh() -> dict:
    return await app.state.orch.refresh_market()


class PositionActionIn(BaseModel):
    utterance: str | None = Field(default=None, max_length=500)
    channel: Literal["voice", "button"] = "voice"


@app.post("/positions/action")
async def positions_action(body: PositionActionIn) -> dict:
    """Sell / close / stop / take-profit change: extract from the user's words, then a confirmation card."""
    s: MandateSession = app.state.session
    _shadow_done(body.utterance)
    if body.channel == "voice":
        _snippet_echo(body.utterance, "position")
    if s.amending and s.amend_kind() != "none":
        return {"ok": False, "reason": "a settings change is in progress; confirm or cancel it first"}
    return await app.state.orch.manage_position(body.utterance, body.channel)


@app.get("/market/ohlcv/{pool}")
async def market_ohlcv(pool: str, tf: str = "15m") -> dict:
    """K-line candles (numbers only) for a pool the session already knows; never an arbitrary address."""
    from backend.market.ohlcv import fetch_ohlcv
    o: Orchestrator = app.state.orch
    known = {x.get("pool") for x in o.offers} | {x.get("pool") for x in o.candidates} | {x.get("pool") for x in o.majors}
    if o.engine is not None:
        known |= {p.pool for p in o.engine.positions.values()}
    if pool not in known - {None}:
        raise HTTPException(404, "unknown pool")
    return await fetch_ohlcv(pool, tf)


class PriceTestIn(BaseModel):
    kind: Literal["take_profit", "stop_loss"]
    utterance: str | None = Field(default=None, max_length=500)
    channel: Literal["voice", "button"] = "button"


@app.post("/test/price-move")
async def price_move(body: PriceTestIn) -> dict:
    """Rehearsal only, local fork only: a whale wallet pushes the pool price to hit the take-profit or stop-loss line."""
    if body.channel == "voice":
        _check_echo(body.utterance, "price_test")
    return await app.state.orch.price_test(body.kind, body.utterance, body.channel)


# ---------------- M6: voice / event push / orchestration ----------------

@app.get("/voice/signed-url")
async def voice_signed_url() -> dict:
    """The backend trades the ElevenLabs API key for a session signed URL. The key never leaves the backend."""
    from backend.voice.elevenlabs import VoiceConfigError, get_signed_url
    try:
        return {"signed_url": await get_signed_url()}
    except VoiceConfigError as e:
        raise HTTPException(502, str(e))


@app.get("/state")
async def state() -> dict:
    """Overall state for the frontend panels (readable before the engine starts)."""
    s: MandateSession = app.state.session
    mandate = ({"frozen": True, "mandate": s.frozen.model_dump(mode="json")} if s.frozen else
               {"frozen": False, "draft": s.draft, "missing_required": s.missing_required(), "stage": s.stage(),
                "recap": s.recap() if not s.missing_required() else None})
    mandate["amending"] = s.amending
    if s.amending:
        mandate["amend_kind"] = s.amend_kind()
        mandate["amend_changes"] = s.amend_changes()
        mandate["active"] = s.base.model_dump(mode="json")
    o = app.state.orch
    st = o.status()
    if st["engine"] is None and app.state.engine is not None:
        st["engine"] = app.state.engine.status()
    sh = app.state.shadow
    return {"mandate_session": mandate, **st, "voice_configured": bool(_voice_ready()), "hearing": sh.hearing(),
            "shadow": {**sh.stats, "extract": {k: v for k, v in (s.spec.stats if s.spec else {}).items()}}}


class PartialIn(BaseModel):
    text: str = Field(max_length=2000)
    final: bool = False
    seq: int = Field(default=0, ge=0)
    mode: str | None = None  # "idle" | "engaged" (from the browser state machine)
    answering: bool = False  # the agent just asked a question and this is her reply
    agent_said: str | None = Field(default=None, max_length=300)  # the agent's last line, context for the addressee check


@app.post("/voice/partial")
async def voice_partial(body: PartialIn) -> dict:
    """Shadow listener: the user's live (partial) or just-finalized words. Staging + free prework only; nothing commits.
    The words are not logged. When mode=engaged and the words are ambiguous, a cheap addressee check may run."""
    from backend.voice.engagement import addressee_needed, addressed_deterministic, classify_addressee, has_wake_name
    out = app.state.shadow.hear(body.text, body.final, body.seq)
    mode = "engaged" if body.mode == "engaged" else "idle"
    if body.final and (mode == "engaged" or has_wake_name(body.text)):
        _loosen_yes_hint(body.text)
    sure = addressed_deterministic(body.text, mode)
    out["wake_name"] = has_wake_name(body.text)
    if sure is not None:
        out["addressed"] = sure
        # side_talk stays the existing "talking to someone else" hint; IDLE+no-name is not side talk
        if mode == "engaged" and sure is False:
            out["side_talk"] = True
        return out
    if mode == "engaged" and body.answering:
        out["addressed"] = True  # a reply to the agent's own question is for the agent
        return out
    if body.final and addressee_needed(body.text, mode):
        addressed = await classify_addressee(body.text, agent_said=body.agent_said)
        out["addressed"] = addressed
        out["side_talk"] = not addressed
        out["addressee_llm"] = True
        return out
    out["addressed"] = True  # ENGAGED and unsure: prefer responding
    return out


YES_ONLY_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|go|go ahead|do it|sounds good)\b", re.I)


def _loosen_yes_hint(text: str) -> None:
    """A pending loosening needs the word "confirm". If the user answers it with a plain "yes", say so out loud
    (the agent may stay silent otherwise). Nothing is applied here."""
    s: MandateSession = app.state.session
    if not s.amending or s.amend_direction() != "loosen" or CONFIRM_WORD_RE.search(text or "") or not YES_ONLY_RE.search(text or ""):
        return
    now = time.time()
    if now - getattr(app.state, "loosen_hint_at", 0.0) < 8:
        return
    app.state.loosen_hint_at = now
    app.state.bus.emit("amend_confirm_needed",
                       "To raise a safety limit I need the word confirm. Say confirm to apply it, or cancel to keep it as it is.",
                       {"changes": s.amend_changes()}, loud=True)


def _voice_ready() -> bool:
    from backend.config import settings
    return bool(settings.elevenlabs_api_key and settings.elevenlabs_agent_id)


@app.get("/agent/status")
async def agent_status() -> dict:
    """get_status tool: only numbers, codes and our own fixed labels."""
    s: MandateSession = app.state.session
    out = app.state.orch.agent_status()
    if s.amending:
        out["mandate"] = {"frozen": True, "changing_settings": True, "change_kind": s.amend_kind()}
    elif not s.frozen:
        out["mandate"] = {"frozen": False, "missing": s.missing_required(), "stage": s.stage()}
    else:
        out["mandate"] = {"frozen": True, "amendable": True}
    return out


class VoiceCardIn(BaseModel):
    action: Literal["approve", "reject", "modify"]
    code: str | None = Field(default=None, pattern=r"^\d{2}$")   # the agent's code is only used to find the card; approval evidence comes from utterance
    utterance: str | None = Field(default=None, max_length=500)
    amount_usdc: float | None = Field(default=None, gt=0)
    # duplex voice: by default long work runs in the background and the outcome arrives as a system update; wait=True blocks (scripts / tests)
    wait: bool = False


@app.post("/voice/card-respond")
async def voice_card_respond(body: VoiceCardIn) -> dict:
    _shadow_done(body.utterance)
    """respond_to_card tool: find the pending card by code, then run M5's raw-words + code check."""
    e = _engine()
    await e.expire_due()
    pending = [c for c in e.cards.values() if c.status == "pending"]
    if body.code:
        card = next((c for c in pending if c.code == body.code), None)
        if card is None:
            return {"ok": False, "reason": f"no pending card with code {body.code}", "pending_codes": [c.code for c in pending]}
    elif len(pending) == 1:
        card = pending[0]
    elif body.action == "approve" and len([c for c in pending if c.offer]) == 1:
        card = next(c for c in pending if c.offer)       # "yes" answers the co-pilot's open de-risking offer
    else:
        return {"ok": False, "reason": f"there are {len(pending)} pending cards; please say the code", "pending_codes": [c.code for c in pending]}
    if body.action != "reject":
        _check_echo(body.utterance, "card")
    if body.action == "modify" and body.amount_usdc is None:
        raise HTTPException(400, "changing the amount needs amount_usdc")
    r = await e.respond(card.id, body.action, utterance=body.utterance, amount_usdc=body.amount_usdc, channel="voice",
                        background=not body.wait)
    return {k: v for k, v in r.items() if k in ("ok", "status", "reason", "signature", "deviation_pct", "executing", "code")}


class TopupIn(BaseModel):
    action: Literal["approve", "reject"]
    amount_usdc: float | None = Field(default=None, gt=0, le=5)
    utterance: str | None = Field(default=None, max_length=500)
    channel: Literal["voice", "button"] = "voice"


@app.post("/research/topup")
async def research_topup(body: TopupIn) -> dict:
    if body.channel == "voice" and body.action == "approve":
        _check_echo(body.utterance, "topup")
    return app.state.orch.respond_topup(body.action, body.amount_usdc, body.utterance, body.channel)


@app.get("/spend")
async def spend() -> dict:
    return app.state.orch.status()["spend"] or {"budget_usdc": None, "spent_usdc": 0.0, "entries": [], "topups": []}


@app.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    """Event push: on connect send recent history first (replay=true, not broadcast by the frontend), then live events."""
    await ws.accept()
    bus: EventBus = app.state.bus
    q = bus.queue()
    try:
        await ws.send_json({"kind": "hello", "history": [e.as_dict() for e in bus.history[-200:]]})
        while True:
            ev = await q.get()
            await ws.send_json({"kind": "event", **ev.as_dict()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unqueue(q)
        with contextlib.suppress(Exception):
            await ws.close()

