"""M1 real-call smoke test: a few real sentences through LLM extraction + JEV explicitness + recap + freeze.

Usage: python scripts/m1_smoke.py      (reads LLM_API_KEY / JEV_API_KEY from .env, never prints them)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.judge.jev import JEVClient  # noqa: E402
from backend.judge.llm import chat_json  # noqa: E402
from backend.mandate.dialogue import MandateSession, verify_hash  # noqa: E402

TURNS = [
    (1, "50 USDC in total, at most 10 bucks per trade, fees whatever you think"),
    (1, "fees at most 0.002 SOL per trade"),
    (2, "raise the market cap cap to 50 million, slippage up to 20%, skip the buy-then-sell test. Stop loss 30%, sell half when it doubles"),
]


async def main() -> None:
    s = MandateSession(extractor=chat_json, judge=JEVClient())
    for rnd, utt in TURNS:
        print(f"\n=== round {rnd} user: {utt}")
        res = await s.submit(utt, rnd)
        print("LLM extraction:", json.dumps(res.extracted, ensure_ascii=False))
        for o in res.outcomes:
            p = "" if o.p_explicit is None else f" P(explicit)={o.p_explicit:.2f}"
            print(f"  [{o.status}] {o.path} = {json.dumps(o.value, ensure_ascii=False)}{p} {o.reason}")
        print("  missing required:", res.missing_required)
        print("  speak:", res.speak)
    rc = s.recap()
    if not rc["ready"]:
        print("\nincomplete, cannot recap:", rc["missing_required"])
        return
    print("\n=== recap\n" + rc["text"])
    m = s.confirm("confirm", rc["digest"])
    print(f"\n=== frozen {m.mandate_id} frozen_at={m.frozen_at} expires_at={m.expires_at}\n  hash={m.hash} verify={verify_hash(m)}")


if __name__ == "__main__":
    asyncio.run(main())
