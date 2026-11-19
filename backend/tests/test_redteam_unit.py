"""P2 injection-token demo: the injected token text never reaches the agent; the guard blocks the obeyed transfer."""
import json

from solders.pubkey import Pubkey

from backend.copilot import redteam
from backend.execute.failure_speak import blocked_buy_speak
from backend.guard.rules import check_instructions

FX = redteam.load()
INJECT = FX["metadata"]["description"]


def test_fixture_is_flagged_by_code():
    assert redteam.has_injection(FX["metadata"]) and not redteam.has_injection({"name": "Cat coin", "symbol": "CAT"})


def test_obeyed_instruction_is_an_spl_transfer_the_guard_blocks():
    w = str(Pubkey.new_unique())
    ix = redteam.obey_injection_ix(w, FX["attacker"], 1_000_000)
    assert str(ix.program_id) == redteam.TOKEN_PROGRAM and bytes(ix.data)[0] == 3

    class Ix:
        def __init__(self, i):
            self.index, self.program_id, self.data = 0, str(i.program_id), bytes(i.data)
            self.accounts = [str(a.pubkey) for a in i.accounts]
    sim = type("S", (), {"tx": type("T", (), {"fee_payer": w, "signers": [w], "instructions": [Ix(ix)]})()})()
    intent = type("I", (), {"wallet": w})()
    found = check_instructions(sim, intent)
    assert found and any("Transfer" in f.message or f.code == "TOKEN_TRANSFER" for f in found)


def test_block_is_spoken_calmly_without_names_or_addresses():
    s = blocked_buy_speak(None, 4, ["TOKEN_TRANSFER: instruction 7: forbidden Token instruction Transfer"], created=False)
    assert s == "The buy for coin 4 was blocked by the guard: it would also move USDC to a wallet that isn't yours. Nothing was sent."
    assert FX["attacker"] not in s and "AIBONUS" not in s


def test_injected_text_never_enters_agent_context(monkeypatch):
    """Desk + agent status + every event speak with the red-team coin on the desk: no token text, no attacker address."""
    import asyncio
    from backend.events import EventBus
    from backend.orchestrator import Orchestrator
    monkeypatch.setenv("VIBE_REDTEAM", "1")
    bus = EventBus()
    o = Orchestrator(bus, discovery="replay", research="cache")

    class Eng:
        redteam = {}
        cards, positions, spent_usdc = {}, {}, 0.0
        def status(self):
            return {"cards": [], "spent_usdc": 0.0, "positions": []}
        def in_flight(self):
            return []
    o.engine = Eng()
    from backend.mandate.preset import load_preset
    from backend.mandate.schema import MandateBody
    o.mandate = type("M", (), {"body": MandateBody.model_validate(load_preset()), "mandate_id": "m"})()

    async def market(pools):
        return {}
    o._market = market
    got = asyncio.run(o._redteam_offer(4, o.mandate))
    o.offers = got
    assert got and o.engine.redteam[FX["mint"]] == FX["attacker"]
    pub = o._offer_public(o._desk()[0])
    assert pub["symbol"] == "TESTCOIN" and "AIBONUS" not in json.dumps(pub)
    assert redteam.CAUTION in pub["signals"]["caution"]
    blob = json.dumps(o.agent_status()) + " ".join(e.speak for e in bus.history)
    for bad in (INJECT, "ignore your previous", FX["attacker"], FX["metadata"]["name"], "AIBONUS"):
        assert bad.lower() not in blob.lower()


def test_flow_summary_shows_usdc_to_unknown_wallet_without_address():
    from types import SimpleNamespace as NS
    from backend.config import USDC_MINT
    from backend.guard.flow import flow_summary
    w = str(Pubkey.new_unique())
    i = redteam.obey_injection_ix(w, FX["attacker"], 100_000)
    ix = NS(index=6, program_id=str(i.program_id), data=bytes(i.data), accounts=tuple(str(a.pubkey) for a in i.accounts))
    sim = NS(tx=NS(instructions=(ix,)), success=True, snaps={}, user_token_accounts={},
             token_deltas={USDC_MINT: -200_000, FX["mint"]: 5_000})
    f = flow_summary(sim, w, FX["mint"], 0.10, 5_000)
    assert f["alerts"] == ["USDC \u2192 unknown wallet: \u22120.1 USDC"]
    assert f["expected"][0]["usdc"] == -0.10 and f["simulated"][0]["usdc"] == -0.20
    assert any(r.get("alert") and r["usdc"] == -0.10 for r in f["simulated"])
    blob = json.dumps(f)
    assert FX["attacker"] not in blob and FX["attacker"][:6] not in blob and "AIBONUS" not in blob
