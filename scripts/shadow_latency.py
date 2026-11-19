"""Measure what the shadow listener saves: end of speech -> filter change applied, with and without it (rehearsal).

Timeline per trial (numbers measured on the live Realtime agent with scripts/partials_probe.py):
  end of speech --(ASR_FINAL_S)--> finalized user_transcript in the browser --(AGENT_TOOL_S)--> submit_mandate_fields
"before": the browser does nothing at the final transcript; "after": it POSTs /voice/partial (final=true) at once,
which primes the mandate extractor. Both then call /mandate/fields with the same words after AGENT_TOOL_S.
Needs scripts/dev.sh (rehearsal). Uses the real LLM extractor (a few small calls), no ElevenLabs, no x402.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ASR_FINAL_S = 0.43     # speech end -> user_transcript (partials_probe, Realtime v1 Max)
AGENT_TOOL_S = 1.12    # user_transcript -> client_tool_call (same probe)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5173/api")
    ap.add_argument("--trials", type=int, default=4)
    a = ap.parse_args()
    async with httpx.AsyncClient(base_url=a.base, timeout=120) as c:
        await c.post("/mandate/reset")
        await c.post("/mandate/fields", json={"utterance": "I want to put in 50 dollars total, and at most 10 dollars per trade.", "round": 1})
        await c.post("/mandate/fields", json={"utterance": "Looks good.", "round": 2})
        r2 = (await c.post("/mandate/fields", json={"utterance": "Stop loss at 30 percent, and sell half when it hits 2x.", "round": 2})).json()
        await c.post("/mandate/confirm", json={"utterance": "Confirm.", "recap_digest": r2["recap"]["digest"]})

        async def idle():
            for _ in range(240):
                st = (await c.get("/state")).json()
                if not st.get("round_running") and st["phase"] not in ("discovery", "research"):
                    return
                await asyncio.sleep(0.25)
        rows = []
        vals = iter(range(310, 900, 7))
        for i in range(a.trials * 2):
            await idle()
            mode = "after" if i % 2 else "before"
            u = f"Another research round with market cap below {next(vals)} thousand."
            t0 = time.perf_counter()
            if mode == "after":
                await c.post("/voice/partial", json={"text": u, "final": True, "seq": 1000 + i})
            await asyncio.sleep(AGENT_TOOL_S - (time.perf_counter() - t0))
            t_tool = time.perf_counter()
            r = (await c.post("/mandate/fields", json={"utterance": u, "round": 2})).json()
            t1 = time.perf_counter()
            rows.append({"mode": mode, "applied": r.get("applied"), "tool_ms": round(1000 * (t1 - t_tool)),
                         "speech_end_to_applied_ms": round(1000 * (ASR_FINAL_S + t1 - t0))})
            print(rows[-1])
        st = (await c.get("/state")).json()
    out = {}
    for m in ("before", "after"):
        xs = [r["speech_end_to_applied_ms"] for r in rows if r["mode"] == m and r["applied"]]
        ts = [r["tool_ms"] for r in rows if r["mode"] == m and r["applied"]]
        out[m] = {"n": len(xs), "median_speech_end_to_applied_ms": statistics.median(xs) if xs else None,
                  "median_tool_call_ms": statistics.median(ts) if ts else None}
    print(json.dumps(out, indent=2))
    print("shadow stats:", json.dumps(st.get("shadow")))
    p = ROOT / "runs" / f"shadow_latency_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps({"rows": rows, "summary": out, "shadow": st.get("shadow"),
                             "assumed": {"asr_final_s": ASR_FINAL_S, "agent_tool_s": AGENT_TOOL_S}}, indent=2), encoding="utf-8")
    print(f"saved {p}")


if __name__ == "__main__":
    asyncio.run(main())
