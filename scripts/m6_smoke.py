"""M6 smoke test: through the Vite proxy (same path as the browser), text channel, start to card; --approve also approves, fills and runs the emergency exit.

Needs scripts/dev.sh running (rehearsal mode: replayed discovery + research cache only, no real money). Trading only on the local Surfpool fork.
  python scripts/m6_smoke.py [--base http://127.0.0.1:5173/api] [--approve]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
R1 = "I want to put in 50 dollars total, and at most 10 dollars per trade."   # no fee: the code default 0.002 SOL applies
R_FILTERS = "Looks good."
R2 = "Stop loss at 30 percent, and sell half when it hits 2x."


def _fork_rss_mb(port: int = 8899) -> float | None:
    """RSS of the Surfpool process serving the main fork (Linux / WSL), for the memory checks."""
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", f"surfpool start --port {port}"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
        return round(int(open(f"/proc/{out[0]}/status", encoding="utf-8").read().split("VmRSS:")[1].split()[0]) / 1024, 1) if out else None
    except (OSError, IndexError, ValueError):
        return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5173/api")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--amend", action="store_true", help="after the sell: change a filter by voice words and run a new research round")
    ap.add_argument("--hold", type=float, default=0, help="with --approve: hold the position this many seconds before selling, sampling fork RSS")
    ap.add_argument("--price-test", choices=["take_profit", "stop_loss"], help="with --approve: push the fork price and expect the listener to sell")
    ap.add_argument("--interrupt", action="store_true", help="with --amend: change a filter mid-round (replan), then stop a round mid-way")
    ap.add_argument("--chart", action="store_true", help="fetch K-line candles for the first desk coin (GeckoTerminal, free)")
    a = ap.parse_args()
    events: list[dict] = []
    out: dict = {"base": a.base, "steps": []}

    def step(name, **kw):
        print(f"[{time.strftime('%H:%M:%S')}] {name} {json.dumps(kw, ensure_ascii=False)[:300]}")
        out["steps"].append({"name": name, **kw})

    async with httpx.AsyncClient(base_url=a.base, timeout=120) as c:
        assert (await c.post("/mandate/reset")).json() == {"ok": True}
        ws_url = a.base.replace("http", "ws", 1) + "/events"
        ws = await websockets.connect(ws_url)
        hello = json.loads(await ws.recv())
        step("WS connected (via proxy)", hello=hello["kind"], history=len(hello["history"]))

        async def pump():
            async for m in ws:
                d = json.loads(m)
                if d.get("kind") == "event":
                    events.append(d)
                    if d["speak"]:
                        print(f"    [event] {d['type']}: {d['speak'][:140]}")
        task = asyncio.create_task(pump())

        async def wait_for(pred, timeout=a.timeout):
            end = time.time() + timeout
            while time.time() < end:
                hit = next((e for e in events if pred(e)), None)
                if hit:
                    return hit
                await asyncio.sleep(0.3)
            raise TimeoutError("timed out waiting for an event")

        sig = (await c.get("/voice/signed-url"))
        step("signed URL", status=sig.status_code, ok=sig.json().get("signed_url", "").startswith("wss://"))
        r1 = (await c.post("/mandate/fields", json={"utterance": R1, "round": 1})).json()
        step("round 1", speak=r1["speak"], stage=r1.get("stage"), missing=r1["missing_required"])
        rf = (await c.post("/mandate/fields", json={"utterance": R_FILTERS, "round": 2})).json()
        step("filter review", speak=rf["speak"], stage=rf.get("stage"))
        r2 = (await c.post("/mandate/fields", json={"utterance": R2, "round": 2})).json()
        step("round 2", speak=r2["speak"][:200], ready=bool(r2.get("recap", {}).get("ready")))
        if not r2.get("recap"):
            sys.exit("required fields missing, see above")
        echo = await c.post("/mandate/confirm", json={"utterance": "[System update] confirm", "recap_digest": r2["recap"]["digest"]})
        step("echoed broadcast confirm refused", status=echo.status_code)
        cf = (await c.post("/mandate/confirm", json={"utterance": "Confirm.", "recap_digest": r2["recap"]["digest"]})).json()
        step("frozen", mandate_id=cf["mandate"]["mandate_id"], hash=cf["mandate"]["hash"][:20])
        await wait_for(lambda e: e["type"] == "free_check_result")
        done = await wait_for(lambda e: e["type"] == "research_done")
        step("research done", speak=done["speak"], spent=done["panel"]["spent_usdc"])
        ask = done if "No coin passed" in (done["speak"] or "") else await wait_for(
            lambda e: e["type"] == "desk_ready" or (e["type"] == "pipeline_status" and e["panel"].get("phase") == "error"))
        card_ev = ask
        if ask["type"] == "desk_ready":
            st = (await c.get("/state")).json()
            desk = st["desk"]
            step("selection desk", speak=ask["speak"], coins=[{"n": o["number"], "live": o["market_live"], "signals": o["signals"],
                                                                 **{k: o["metrics"].get(k) for k in ("market_cap", "liquidity_usd", "volume_to_liquidity",
                                                                                                     "price_change_h1", "holders", "top10_pct")}}
                                                                for o in desk["offers"]])
            pools = [o["pool"] for o in desk["offers"] if o["pool"]]
            if pools and a.chart:
                ch = (await c.get(f"/market/ohlcv/{pools[0]}?tf=15m")).json()
                step("chart candles", ok=ch.get("ok"), n=len(ch.get("candles") or []), source=ch.get("source"), reason=ch.get("reason"))
            n = len(desk["offers"])
            words = "Coin 1 and coin 2, 5 dollars each." if n >= 2 else "Coin 1, 10 dollars."
            od = (await c.post("/desk/order", json={"utterance": words, "channel": "voice"})).json()
            # duplex: the desk order returns at once (status=building_cards); the codes arrive with cards_ready
            step("desk order", words=words, ok=od.get("ok"), reason=od.get("reason"), status=od.get("status"), orders=od.get("orders"),
                 working_on=(await c.get("/agent/status")).json().get("working_on"))
            card_ev = await wait_for(lambda e: e["type"] in ("cards_ready", "guard_blocked") or
                                     (e["type"] == "pipeline_status" and e["panel"].get("phase") == "error"))
        st = (await c.get("/state")).json()
        cards = [x for x in (st["engine"] or {}).get("cards", []) if x["status"] == "pending"]
        step("cards", n=len(cards), first=card_ev["type"],
             cards=[{"code": x["code"], "guard": x["guard"]["decision"], "jev": x["guard"]["jev_risk"],
                     "impact": x["sim"]["price_impact_pct"]} for x in cards])
        ag = (await c.get("/agent/status")).json()
        step("agent status (numbers only)", status=ag)
        if a.approve and cards:
            code = cards[0]["code"]
            bad = (await c.post("/voice/card-respond", json={"action": "approve", "code": code, "utterance": "approve"})).json()
            step("no code", ok=bad["ok"], reason=bad.get("reason"))
            ok = (await c.post("/voice/card-respond", json={"action": "approve", "code": code, "utterance": f"Approve {code}."})).json()
            step("approve (returns before the swap)", **ok)
            if ok.get("ok"):
                fill = await wait_for(lambda e: e["type"] in ("trade_executed", "trade_failed") and e["panel"].get("card_id") == cards[0]["id"], 120)
                step("fill (background)", type=fill["type"], speak=fill["speak"])
                if a.hold:
                    samples = []
                    t0 = time.time()
                    while time.time() - t0 < a.hold:
                        ag = (await c.get("/agent/status")).json()
                        mult = [p.get("price_vs_entry_multiple") for p in ag.get("positions", [])]
                        samples.append((round(time.time() - t0), _fork_rss_mb(), mult))
                        await asyncio.sleep(10)
                    step("held position (listener polling)", seconds=a.hold, fork_rss_mb=samples)
                if a.price_test:
                    pt = (await c.post("/test/price-move", json={"kind": a.price_test, "utterance": f"simulate {a.price_test.replace('_', ' ')}",
                                                                  "channel": "voice"})).json()
                    sold = await wait_for(lambda e: e["type"] in ("exit_triggered", "sell_failed", "price_feed_down"), 120)
                    step("price test", start=pt, first=sold["type"], speak=sold["speak"], fork_rss_mb=_fork_rss_mb())
                ex = await c.post("/emergency-exit", json={"utterance": "Sell all.", "channel": "voice"})
                step("emergency exit (returns before the sells)", status=ex.status_code, result=ex.json())
                end = time.time() + 120
                while time.time() < end and (await c.get("/agent/status")).json().get("working_on"):
                    await asyncio.sleep(0.5)
                step("exit finished", working_on=(await c.get("/agent/status")).json().get("working_on"))
            for x in cards[1:]:
                await c.post("/voice/card-respond", json={"action": "reject", "code": x["code"]})
            st = (await c.get("/state")).json()
            step("after the sell", phase=st["phase"], amend=st.get("amend"))
            if a.amend:
                am = (await c.post("/mandate/fields", json={"utterance": "Another research round with market cap below 400,000.", "round": 2})).json()
                step("amend + new round", speak=am.get("speak"), applied=am.get("applied"), blocked=am.get("blocked"))
                if am.get("applied"):
                    ev2 = await wait_for(lambda e: e["type"] == "research_done" and e["panel"].get("round") == 2)
                    st2 = (await c.get("/state")).json()
                    step("round 2", speak=ev2["speak"], versions=st2.get("mandate_versions"), round=st2.get("round"),
                         desk=len(st2["desk"]["offers"]), phase=st2["phase"],
                         mcap_max=st2["mandate"] and st2["mandate_session"]["mandate"]["body"]["filters"]["market_cap_usd"][1])
                if am.get("applied") and a.interrupt:
                    # duplex 1: change a filter while the round runs -> the old round stops, the new one starts
                    a3 = (await c.post("/mandate/fields", json={"utterance": "Another research round with market cap below 300,000.", "round": 2})).json()
                    mid = (await c.get("/state")).json()
                    a4 = (await c.post("/mandate/fields", json={"utterance": "Actually, another round with market cap below 250,000.", "round": 2})).json()
                    step("replan mid-round", first=a3.get("applied"), second=a4.get("applied"), blocked=a4.get("blocked"),
                         round_running_before=mid.get("round_running"), working_on=mid.get("working_on"))
                    n_stop = len([e for e in events if e["type"] == "research_stopped"])
                    await asyncio.sleep(0.2)
                    # duplex 2: "stop the research"
                    sr = (await c.post("/research/stop", json={"utterance": "Stop the research.", "channel": "voice"})).json()
                    await asyncio.sleep(0.5)
                    st3 = (await c.get("/state")).json()
                    step("stop research", result=sr, phase=st3["phase"], round_running=st3.get("round_running"),
                         desk=len(st3["desk"]["offers"]), stopped_events=len([e for e in events if e["type"] == "research_stopped"]) - n_stop)
            st = (await c.get("/state")).json()
            step("end", trades=[{k: t.get(k) for k in ("kind", "trigger", "signature", "spent", "received", "error")}
                                for t in st["engine"]["trades"]],
                 positions=[{k: p[k] for k in ("entry_usdc", "realized_usdc", "held_tokens", "status")} for p in st["engine"]["positions"]])
        task.cancel()
        await ws.close()
    out["events"] = events
    p = ROOT / "runs" / f"m6_smoke_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {p}")


if __name__ == "__main__":
    asyncio.run(main())
