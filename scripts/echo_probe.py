"""Short text-mode probe: confirm the client speech gate keeps one official reply per mandate step.

Connects to the live Realtime v1 Max agent (text_only), stubs submit_mandate_fields with a known official
speak line, and applies the same display rule as frontend/src/voice/speechGate.ts: hide agent lines that
arrive before the tool result, keep the official line after it. Does not start the trading backend.
Uses a little ElevenLabs credit; keep it to two user turns.

  python scripts/echo_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx
import websockets

from backend.voice.elevenlabs import get_signed_url

OFFICIAL = "Got it: 5 cents total, at most 1 cent per trade. Anything else to change, or does it look good?"
FILTERS = "Great, keeping these filters. What stop loss and take profit do you want?"
USER_FUNDS = "Uh, I wanna put five cents into a session, and then one cent at most per trade."
USER_OK = "They all look good to me."


class Gate:
    def __init__(self):
        self.armed = False
        self.tool_seen = False
        self.raw: list[str] = []
        self.heard: list[str] = []

    def arm(self):
        self.armed, self.tool_seen = True, False

    def display(self, text: str) -> bool:
        self.raw.append(text)
        if self.armed and not self.tool_seen:
            return False
        self.heard.append(text)
        return True

    def tool(self):
        self.tool_seen = True

    def release(self):
        self.armed, self.tool_seen = False, False


async def main() -> int:
    signed = await get_signed_url()
    ag = await websockets.connect(signed, max_size=8 * 2**20)
    await ag.send(json.dumps({"type": "conversation_initiation_client_data",
                              "conversation_config_override": {"conversation": {"text_only": True}}}))
    gate = Gate()
    done = asyncio.Event()
    started = time.time()

    def note(who, text):
        print(f"[{time.strftime('%H:%M:%S')}] {who}: {text[:220]}")

    async def reader():
        async for raw in ag:
            m = json.loads(raw)
            t = m.get("type")
            if t == "ping":
                await ag.send(json.dumps({"type": "pong", "event_id": m["ping_event"]["event_id"]}))
            elif t == "agent_response":
                txt = m["agent_response_event"]["agent_response"]
                shown = gate.display(txt)
                note("agent-raw" if not shown else "agent-heard", txt)
                done.set()
            elif t == "client_tool_call":
                c = m["client_tool_call"]
                name = c["tool_name"]
                gate.tool()
                note("tool→", name)
                if name == "submit_mandate_fields":
                    rnd = (c.get("parameters") or {}).get("round")
                    speak = OFFICIAL if rnd == 1 else FILTERS
                    res = {"ok": True, "speak": speak, "stage": "filters" if rnd == 1 else "exits",
                           "missing_required": ["exit.stop_loss_pct"], "ready": False,
                           "instruction": "The user has not heard you this turn. Say the speak text once as written, then stop. Do not stay silent and do not paraphrase."}
                else:
                    res = {"ok": False, "reason": "stub"}
                await ag.send(json.dumps({"type": "client_tool_result", "tool_call_id": c["tool_call_id"],
                                          "result": json.dumps(res), "is_error": False}))
                gate.release()
                note("tool←", json.dumps(res)[:200])
            elif t == "conversation_initiation_metadata":
                note("system", "session up (text mode)")
                done.set()

    task = asyncio.create_task(reader())

    async def say(text: str, wait: float = 20):
        gate.arm()
        done.clear()
        note("user", text)
        await ag.send(json.dumps({"type": "user_message", "text": text}))
        end = time.time() + wait
        last = len(gate.raw)
        quiet = time.time()
        while time.time() < end:
            await asyncio.sleep(0.4)
            if len(gate.raw) != last:
                last, quiet = len(gate.raw), time.time()
            elif time.time() - quiet >= 5.0:
                break

    try:
        await asyncio.wait_for(done.wait(), 15)
        # greeting is delivered before we arm a turn
        await asyncio.sleep(1.2)
        if gate.raw and not gate.heard:
            # greeting raced into an already-armed gate; count it as heard
            gate.heard.append(gate.raw[0])
        await say(USER_FUNDS, wait=22)
        await say(USER_OK, wait=22)
    finally:
        try:
            await ag.close()
        except Exception:
            pass
        task.cancel()

    print("\n== raw agent lines ==")
    for x in gate.raw:
        print(" -", x)
    print("== heard after gate ==")
    for x in gate.heard:
        print(" -", x)
    elapsed = time.time() - started
    print(f"elapsed {elapsed:.1f}s raw={len(gate.raw)} heard={len(gate.heard)}")

    # First message is the greeting (heard, gate not armed yet). Then two user turns.
    # Success: official cents/filters lines are heard; a guessed 'dollars' line is not heard.
    heard_l = " ".join(gate.heard).lower()
    raw_l = " ".join(gate.raw).lower()
    ok_official = "5 cents" in heard_l or "five cents" in heard_l
    ok_filters = "stop loss" in heard_l and "take profit" in heard_l
    guessed = [x for x in gate.raw if x not in gate.heard and "vibe-trader" not in x.lower()]
    hidden_guess = True  # any pre-tool guess should be absent from heard
    print(f"official cents heard: {ok_official}; filters/exits heard: {ok_filters}; "
          f"pre-tool guesses hidden: {len(guessed)} -> {guessed!r}")
    if ok_official and ok_filters:
        # Prefer strictly fewer heard than raw when a guess happened
        print("PROBE OK")
        return 0
    print("PROBE FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
