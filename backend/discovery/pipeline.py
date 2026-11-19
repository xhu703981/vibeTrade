"""M3 coin pipeline: discover -> free checks (market + mint + LP) -> buy-then-sell simulation only for coins that pass all of them.

Holder concentration, dev holding, holder count and known-rugger are paid research (M4) fields: marked pending_research, not a pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.config import redact, settings
from backend.discovery.dexscreener import Candidate, DexScreener, pair_metrics
from backend.discovery.free_checks import check_lp, check_market, check_mint, fetch_mints, parse_mint
from backend.discovery.sell_sim import sell_sim
from backend.mandate.schema import Mandate
from backend.solana_utils import Rpc

RESEARCH_FIELDS = ("max_top10_pct", "max_dev_pct", "min_holders", "reject_known_rugger")


@dataclass
class CoinReport:
    mint: str
    symbol_display: str
    sources: list[str]
    paid_promo: bool
    status: str = "pending"            # fail_free | fail_sell_sim | pending_research
    failed_stage: str | None = None    # market | mint | lp | sell_sim
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    lp: dict = field(default_factory=dict)
    sell_sim: dict | None = None
    research_pending: list[str] = field(default_factory=list)


async def run_pipeline(m: Mandate, max_sell_sims: int = 10, log=print, candidates: list | None = None) -> list[CoinReport]:
    """candidates: a ready candidate list (e.g. smart-money net buying); None = DexScreener's trending / boosted lists."""
    ds = DexScreener()
    try:
        cands = list(candidates) if candidates else await ds.discover()
        log(f"found {len(cands)} candidates (Solana)")
        await ds.enrich(cands)
    finally:
        await ds.close()
    rpc = Rpc(settings.solana_rpc_url)
    reports: list[CoinReport] = []
    try:
        mints = await fetch_mints(rpc, [c.mint for c in cands])
        for c in cands:
            rep = CoinReport(mint=c.mint, symbol_display=c.symbol_display, sources=c.sources, paid_promo=c.paid_promo)
            if c.pair:
                rep.metrics = {"dex": c.pair.dex_id, "labels": c.pair.labels, "pair": c.pair.pair_address, **pair_metrics(c.pair)}
            stages: list[tuple[str, list[str]]] = [("market", check_market(c, m))]
            raw = mints.get(c.mint)
            if raw is None:
                stages.append(("mint", ["could not read the mint account"]))
            else:
                try:
                    mi = parse_mint(*raw)
                    stages.append(("mint", check_mint(mi, m)))
                    rep.metrics["token_program"] = "Token-2022" if mi.program.startswith("Tokenz") else "Token"
                    rep.metrics["extensions"] = sorted(mi.extensions)
                    rep.metrics["mint_revoked"] = mi.mint_authority is None
                    rep.metrics["freeze_revoked"] = mi.freeze_authority is None
                except ValueError as e:
                    stages.append(("mint", [f"could not parse mint: {e}"]))
            try:
                lp_r, rep.lp = await check_lp(rpc, c, m)
            except Exception as e:  # noqa: BLE001
                lp_r = [redact(f"LP check error: {type(e).__name__}: {str(e)[:120]}")]
            stages.append(("lp", lp_r))
            for stage, rs in stages:
                if rs:
                    rep.failed_stage = rep.failed_stage or stage
                    rep.reasons += [f"[{stage}] {r}" for r in rs]
            rep.status = "fail_free" if rep.reasons else "pending"
            reports.append(rep)
    finally:
        await rpc.close()

    survivors = [r for r in reports if r.status == "pending"]
    log(f"{len(survivors)} passed free checks; starting buy-then-sell simulations (at most {max_sell_sims})")
    for r in survivors[:max_sell_sims]:
        s = await sell_sim(r.mint, m)
        r.sell_sim = s.as_dict()
        if s.ok:
            r.status = "pending_research"
            r.research_pending = list(RESEARCH_FIELDS)
        else:
            r.status, r.failed_stage = "fail_sell_sim", "sell_sim"
            r.reasons += [redact(f"[sell_sim] {x}") for x in s.reasons]
        log(f"  {r.mint[:8]}… {r.status} {s.roundtrip_loss_pct if s.roundtrip_loss_pct is not None else ''}")
    for r in survivors[max_sell_sims:]:
        r.status, r.reasons = "skipped", ["[sell_sim] over this round's simulation limit, not tested"]
    return reports
