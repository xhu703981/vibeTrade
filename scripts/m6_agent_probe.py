"""Run the real ElevenLabs agent (text mode) end to end, with this script playing the browser frontend.

  - get a signed URL from the backend and connect to the agent WebSocket (text_only override);
  - agent client_tool_call -> call the local backend with the same rules as the frontend (the user's words are
    attached by this script, never taken from agent arguments);
  - backend events -> "[System update] ..." sent to the agent as user_message (same as the frontend's sendUserMessage);
  - user lines: funds (no fee) + "looks good" on the proposed filters + stop loss / take profit + confirm
    + a buy amount ("10 dollars this time") + "approve" without a code + "approve NN" + status + "sell all"
    + duplex: a filter change and "stop the research" while a round is running (tools return fast; outcomes come as updates).
Needs scripts/dev.sh running (rehearsal mode). Uses a little ElevenLabs credit; trading only on the local fork.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
MARK = "[System update]"
URGENT = "[URGENT update]"
SPEAK_SKIP = {"research_payment", "mandate_updated"}

# The spoken demo script (English).
USER_LINES = {
    "round1": "I want to put in 50 dollars total, and at most 10 dollars per trade.",
    "filters": "Looks good.",
    "round2": "Stop loss at 30 percent, and sell half when it hits 2x.",
    "confirm": "Confirm.",
    "desk_order": "Let's take coin 1, 10 dollars.",
    "amend": "Now I want another research round with market cap below 400,000.",
    "approve_no_code": "Approve.",
    "approve": "Approve {code}.",
    "status": "What's the status? How much did research cost?",
    "sell_all": "Sell all.",
    "replan": "Wait, make that market cap below 300,000 instead, and run it.",
    "stop": "Actually, stop the research.",
    "side_talk": "Hey mom, is dinner ready later?",
}
SIDE_TALK_NOTE = "On-screen note: the user's last words look like side talk to someone else, not to you. Stay silent unless they were for you."


class Probe:
    def __init__(self, base: str):
        self.http = httpx.AsyncClient(base_url=base, timeout=120)
        self.base = base
        self.last_user: str | None = None
        self.used = False
        self.digest: str | None = None
        self.log: list[dict] = []
        self.tool_calls: list[dict] = []
        self.agent_lines: list[str] = []
        self.events: list[dict] = []
        self.busy = 0
        self.agent_turn_done = asyncio.Event()

    def note(self, who, text):
        print(f"[{time.strftime('%H:%M:%S')}] {who}: {text[:220]}")
        self.log.append({"t": time.time(), "who": who, "text": text})

    async def tool(self, name: str, p: dict):
        u = self.last_user
        try:
            if name == "submit_mandate_fields":
                r = (await self.http.post("/mandate/fields", json={"utterance": u, "round": 1 if p.get("round") == 1 else 2})).json()
                self.digest = (r.get("recap") or {}).get("digest")
                return {"ok": True, "speak": r.get("speak"), "missing_required": r.get("missing_required"), "ready": bool(r.get("recap"))}
            if name == "confirm_mandate":
                r = await self.http.post("/mandate/confirm", json={"utterance": u, "recap_digest": self.digest or ""})
                return {"ok": r.status_code == 200, **({} if r.status_code == 200 else {"reason": r.json().get("detail")})}
            if name == "respond_to_card":
                code = str(p.get("code") or "").strip() or None
                r = await self.http.post("/voice/card-respond", json={"action": p.get("action"), "code": code, "utterance": u,
                                                                      "amount_usdc": p.get("amount_usdc")})
                return r.json() if r.status_code == 200 else {"ok": False, "reason": r.json().get("detail")}
            if name == "respond_to_topup":
                return (await self.http.post("/research/topup", json={**p, "utterance": u, "channel": "voice"})).json()
            if name == "place_orders":
                nums = [int(x) for x in (p.get("coin_numbers") or []) if str(x).isdigit()]
                each, lst = p.get("amount_usdc_each"), p.get("amounts_usdc") or []
                orders = ([{"coin_number": n, "amount_usdc": float(v)} for n, v in zip(nums, lst)] if lst and len(lst) == len(nums) else
                          [{"coin_number": n, "amount_usdc": float(each)} for n in nums] if isinstance(each, (int, float)) and each > 0 else None)
                r = await self.http.post("/desk/order", json={"orders": orders, "utterance": u, "channel": "voice"})
                return r.json() if r.status_code == 200 else {"ok": False, "reason": r.json().get("detail")}
            if name == "skip_coins":
                return (await self.http.post("/desk/skip", json={"coin_numbers": p.get("coin_numbers"), "utterance": u, "channel": "voice"})).json()
            if name == "simulate_price_move":
                kind = "stop_loss" if p.get("kind") == "stop_loss" else "take_profit"
                return (await self.http.post("/test/price-move", json={"kind": kind, "utterance": u, "channel": "voice"})).json()
            if name == "get_status":
                return (await self.http.get("/agent/status")).json()
            if name == "emergency_exit":
                r = await self.http.post("/emergency-exit", json={"utterance": u, "channel": "voice"})
                return r.json() if r.status_code == 200 else {"ok": False, "reason": r.json().get("detail")}
            if name == "stop_research":
                return (await self.http.post("/research/stop", json={"utterance": u, "channel": "voice"})).json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": type(e).__name__}
        return {"ok": False, "reason": "unknown tool"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5173/api")
    a = ap.parse_args()
    pr = Probe(a.base)
    await pr.http.post("/mandate/reset")
    signed = (await pr.http.get("/voice/signed-url")).json()["signed_url"]
    ag = await websockets.connect(signed, max_size=8 * 2**20)
    await ag.send(json.dumps({"type": "conversation_initiation_client_data",
                              "conversation_config_override": {"conversation": {"text_only": True}},
                              "dynamic_variables": {"greeting": "Hi, I'm Vibe-Trader. Want to change anything before we look at coins?"}}))
    ev_ws = await websockets.connect(a.base.replace("http", "ws", 1) + "/events")
    await ev_ws.recv()  # hello
    broadcast_q: asyncio.Queue = asyncio.Queue()

    async def agent_reader():
        async for raw in ag:
            m = json.loads(raw)
            t = m.get("type")
            if t == "ping":
                await ag.send(json.dumps({"type": "pong", "event_id": m["ping_event"]["event_id"]}))
            elif t == "agent_response":
                txt = m["agent_response_event"]["agent_response"]
                pr.agent_lines.append(txt)
                pr.note("agent", txt)
                pr.agent_turn_done.set()
            elif t == "client_tool_call":
                c = m["client_tool_call"]
                pr.busy += 1
                pr.note("tool→", f"{c['tool_name']} {json.dumps(c.get('parameters'), ensure_ascii=False)}")
                res = await pr.tool(c["tool_name"], c.get("parameters") or {})
                pr.tool_calls.append({"name": c["tool_name"], "params": c.get("parameters"), "result": res,
                                      "utterance": pr.last_user})
                pr.note("tool←", json.dumps(res, ensure_ascii=False))
                await ag.send(json.dumps({"type": "client_tool_result", "tool_call_id": c["tool_call_id"],
                                          "result": json.dumps(res, ensure_ascii=False), "is_error": False}))
                pr.busy -= 1
            elif t == "conversation_initiation_metadata":
                pr.note("system", "agent session established (text mode)")

    async def event_reader():
        async for raw in ev_ws:
            d = json.loads(raw)
            if d.get("kind") == "event":
                pr.events.append(d)
                if d["speak"] and d["type"] not in SPEAK_SKIP:
                    await broadcast_q.put((URGENT if d["loud"] else MARK) + " " + d["speak"])

    seq = [0]

    async def say(text: str, wait_s: float = 25):
        pr.last_user = text
        pr.note("user", text)
        pr.agent_turn_done.clear()
        # like the browser: the finalized words go to the shadow listener at once (staging + free prework only)
        seq[0] += 1
        sh = (await pr.http.post("/voice/partial", json={"text": text, "final": True, "seq": seq[0]})).json()
        if sh.get("hearing"):
            pr.note("hearing", sh["hearing"]["label"])
        if sh.get("side_talk"):
            pr.note("side-talk", "hint sent")
            await ag.send(json.dumps({"type": "contextual_update", "text": SIDE_TALK_NOTE}))
        await ag.send(json.dumps({"type": "user_message", "text": text}))
        await settle(wait_s)

    async def settle(wait_s: float):
        """Wait until the agent is done (4 s quiet after its last reply, no tool running)."""
        end = time.time() + wait_s
        last = len(pr.log)
        quiet_since = time.time()
        while time.time() < end:
            await asyncio.sleep(0.5)
            if len(pr.log) != last or pr.busy:
                last, quiet_since = len(pr.log), time.time()
            elif time.time() - quiet_since > 4 and pr.agent_lines:
                return

    async def flush_broadcasts(max_n=6):
        n = 0
        while not broadcast_q.empty() and n < max_n:
            text = await broadcast_q.get()
            pr.note("broadcast", text)
            pr.agent_turn_done.clear()
            await ag.send(json.dumps({"type": "user_message", "text": text}))
            await settle(20)
            n += 1

    out_code = None
    tasks = [asyncio.create_task(agent_reader()), asyncio.create_task(event_reader())]
    try:
        await settle(10)   # first message
        await say(USER_LINES["round1"])
        await say(USER_LINES["filters"])
        await say(USER_LINES["round2"])
        await say(USER_LINES["confirm"])
        # research passes a coin -> the agent asks how much to buy this time (a few seconds in rehearsal mode)
        for _ in range(60):
            if any(e["type"] == "desk_ready" or (e["type"] == "research_done" and "No coin passed" in e["speak"]) for e in pr.events):
                break
            await asyncio.sleep(1)
        await flush_broadcasts()
        if any(e["type"] == "desk_ready" for e in pr.events):
            await say(USER_LINES["desk_order"])
        # wait for the card
        for _ in range(60):
            if any(e["type"] in ("cards_ready", "card_created") for e in pr.events):
                break
            await asyncio.sleep(1)
        await flush_broadcasts()
        st = (await pr.http.get("/state")).json()
        pend = [c for c in (st.get("engine") or {}).get("cards", []) if c["status"] == "pending"]
        if pend:
            code = pend[0]["code"]
            await say(USER_LINES["approve_no_code"])   # no code: must be refused
            await flush_broadcasts()
            out_code = code
            await say(USER_LINES["approve"].format(code=code), 40)
            for _ in range(30):
                if any(e["type"] in ("trade_executed", "trade_failed", "card_voided") for e in pr.events):
                    break
                await asyncio.sleep(1)
            await flush_broadcasts()
        await say(USER_LINES["status"])
        n_agent = len(pr.agent_lines)
        await say(USER_LINES["side_talk"], 12)
        pr.note("system", f"side talk: agent said {len(pr.agent_lines) - n_agent} line(s)")
        await say(USER_LINES["sell_all"], 40)
        await asyncio.sleep(3)
        await flush_broadcasts()
        # living session: after the sell, change a filter and hunt again (the agent must not call the mandate read-only)
        await say(USER_LINES["amend"], 40)
        for _ in range(60):
            if any(e["type"] in ("mandate_amended", "round_started") for e in pr.events):
                break
            await asyncio.sleep(1)
        # duplex: change the filter again while that round may still run, then stop the research
        await say(USER_LINES["replan"], 30)
        await say(USER_LINES["stop"], 30)
        await asyncio.sleep(2)
        await flush_broadcasts()
    finally:
        for t in tasks:
            t.cancel()
        await ag.close()
        await ev_ws.close()
    st = (await pr.http.get("/state")).json()
    trades = (st.get("engine") or {}).get("trades", [])
    out = {"card_code": out_code, "tool_calls": pr.tool_calls, "log": pr.log, "trades": [{k: t.get(k) for k in ("kind", "trigger", "signature", "spent", "received", "error")} for t in trades]}
    p = ROOT / "runs" / f"m6_agent_probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n== tool calls ==")
    for c in pr.tool_calls:
        print(f"  {c['name']} {json.dumps(c['params'], ensure_ascii=False)} utterance={c['utterance']!r} -> ok={c['result'].get('ok', '-')}")
    print("== trades ==")
    for t in out["trades"]:
        print(" ", t)
    print(f"saved {p}")


if __name__ == "__main__":
    asyncio.run(main())
