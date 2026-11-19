"""Session orchestration: a living portfolio, not a one-shot script.

  mandate frozen -> research round (discovery + free checks -> paid research) -> selection desk (every qualified coin, numbered)
  -> the user picks any number of coins and an amount each -> one confirmation card per coin -> execution + listener
  -> (any time it is safe) amend filters / guards -> next research round. Cash, open positions, trades, cards and the
  research-spend ledger carry over between rounds; open positions keep the exit rules they were bought under.

Single user, in-memory state (enough for a hackathon demo). All text pushed to the UI / voice is code-templated:
speak holds only numbers, codes and our own fixed wording; panel holds only numbers, mint addresses, sanitised symbols and sanitised reasons.

Modes (environment variables):
  VIBE_DISCOVERY=live|replay   live: live DexScreener + Helius discovery; replay: latest runs/m3_*.json (rehearsal)
  VIBE_RESEARCH=live|cache     live: pay real x402 money (cache first); cache: only already-bought cache, never pays (default)
  VIBE_UI_BUTTONS=1            enable the on-screen button channel (desk orders / cards / research top-ups)
  VT_NETWORK=mainnet           trade on Solana MAINNET with real funds (default: the local Surfpool fork). Rehearsal-only
                               features (price tests, the red-team coin, fork cheatcodes) are off on mainnet.
The number of coins you can buy in a round is limited only by the session budget and the per-trade cap.

Duplex conversation (the voice keeps going while work runs): long operations never hold the voice turn. Desk orders
(card simulation), card execution and the emergency exit are validated synchronously (raw words, code, caps) and then
run as background tasks; the real outcome arrives as a [System update] / [URGENT update]. A research round can be
stopped, or replaced by an amended one, while it runs: no new research payment starts after the stop.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path

from backend.config import mainnet_rpc_url, network_status, redact, settings
from backend.discovery.dexscreener import sanitize_symbol
from backend.events import EventBus
from backend.market.majors import all_templates, find_pool, live_prices
from backend.voice.position_action import resolve_position_action

from backend.research import cache
from backend.research.budget import ATOMIC, Ledger
from backend.research.x402_client import PaymentRefused
from backend.voice.desk_order import resolve_desk_order
from backend.copilot.desk import (CAP_WORDS, SUSPICION_ASK, TOP_N, desk_reasons, holder_note, near_limit, parse_risk, spoken, vs_limit,
                                  warning_text, rank_score, size_from_risk, suspicion)
from backend.voice.verify import parse_desk_skip, verify_price_test, verify_sell_all, verify_topup

RUNS = Path(__file__).resolve().parent.parent / "runs"
TOPUP_TIMEOUT_S = 60.0
SELLER_LABEL = {"nansen_holders": "Nansen holders", "quickintel_scan": "Quick Intel security scan", "cmc_dex_search": "CoinMarketCap"}
STATUS_LABEL = {"fail_free": "failed free checks", "fail_sell_sim": "failed buy-then-sell test", "pending_research": "sent to paid research",
                "skipped": "not tested this round", "pass": "passed research", "fail_research": "failed research",
                "research_stopped": "research stopped", "not_researched": "over this round's limit"}
PHASE_AGENT_LABEL = {"mandate": "setting the mandate", "discovery": "research round: discovery and free checks",
                     "research": "research round: paid research", "selecting": "choosing coins on the selection desk",
                     "monitoring": "monitoring open positions", "idle": "between rounds (no open positions)",
                     "error": "stopped after an error"}
DESK_NUM_KEYS = ("market_cap", "liquidity_usd", "age_hours", "volume_h24", "volume_to_liquidity", "price_change_h1",
                 "price_change_h6", "price_change_h24", "buys_h1", "sells_h1", "buys_h24", "sells_h24", "holders", "top10_pct",
                 "dev_pct", "roundtrip_loss_pct", "lp_burned_pct", "lp_locked_pct", "sm_netflow_24h_usd", "sm_traders")
_SAFE = re.compile(r"[^0-9A-Za-z .,%$:;()\[\]=+\-/_~]")  # ASCII only: no quotes, no angle brackets
NUM_KEYS = ("liquidity_usd", "market_cap", "volume_h24", "roundtrip_loss_pct", "top10_pct", "dev_pct", "holders")


def log_traceback(what: str, e: BaseException) -> None:
    """A background failure's full traceback to stderr (logs/backend.err under dev.ps1 / dev.sh), secrets scrubbed.
    Never raises: logging must not turn one failure into two (e.g. a console that can't encode a character)."""
    import sys
    import traceback
    try:
        text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] background {what} failed:\n{redact(text)}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def safe_reason(s: object, n: int = 90) -> str:
    """Reason text is for the UI only: strip odd characters and truncate. Never goes into speak or agent tool results."""
    return _SAFE.sub("", redact(str(s)))[:n]


def _nums(d: dict) -> dict:
    return {k: d[k] for k in NUM_KEYS if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)}


class CacheOnlyPayer:
    """Cache-replay mode: anything not in the cache is never bought (nothing signed, nothing spent)."""
    ledger = None

    async def buy(self, seller, mint, reason, **kw):
        raise PaymentRefused("cache-replay mode never pays (this data is not cached)")

    async def close(self):
        pass


class _StoppablePayer:
    """Research payer wrapper: once the round is stopped, no new payment starts (an in-flight one finishes and is ledgered)."""

    def __init__(self, inner, stopped):
        self.inner, self._stopped = inner, stopped
        self.ledger = getattr(inner, "ledger", None)

    async def buy(self, seller, mint, reason, **kw):
        if self._stopped():
            raise PaymentRefused("the research round was stopped; nothing more is bought")
        return await self.inner.buy(seller, mint, reason, **kw)

    async def close(self):
        await self.inner.close()

    def __getattr__(self, k):
        return getattr(self.inner, k)


class RoundStopped(Exception):
    pass


def _loss_words(usd: float) -> str:
    """Spoken loss, rounded the way people say it: 'about 10 cents', 'about 1.40 dollars'."""
    cents = max(1, round(usd * 100))
    if cents < 100:
        return "1 cent" if cents == 1 else f"{cents} cents"
    return _spoken_amount(round(usd, 2))


def _spoken_amount(usd: float) -> str:
    cents = round(usd * 100)
    if usd < 1 and abs(usd * 100 - cents) < 1e-6:
        return "1 cent" if cents == 1 else f"{cents} cents"
    return "1 dollar" if abs(usd - 1) < 1e-9 else f"{usd:g} dollars"


class Orchestrator:
    def __init__(self, bus: EventBus, *, discovery: str | None = None, research: str | None = None,
                 ui_buttons: bool | None = None,
                 engine_factory=None, discover_fn=None, research_fn=None, payer_factory=None, jev_factory=None,
                 runs_dir: Path | None = None, price_test_fn=None, market_fn=None):
        self.bus = bus
        self.runs = runs_dir or RUNS
        self.discovery_mode = discovery or os.environ.get("VIBE_DISCOVERY", "live")
        self.research_mode = research or os.environ.get("VIBE_RESEARCH", "cache")
        self.ui_buttons = ui_buttons if ui_buttons is not None else os.environ.get("VIBE_UI_BUTTONS", "0") == "1"
        # majors are frozen for the meme-only co-pilot (code kept; VIBE_MAJORS=1 brings the section back)
        self.majors_enabled = os.environ.get("VIBE_MAJORS", "0") == "1"
        self._engine_factory = engine_factory or self._default_engine
        self._discover = discover_fn or self._default_discover
        self._research = research_fn or self._default_research
        self._payer_factory = payer_factory or self._default_payer
        self._jev_factory = jev_factory or self._default_jev
        self._price_test = price_test_fn or self._default_price_test
        self._market = market_fn or self._default_market
        self.reset()

    def reset(self) -> None:
        self.phase = "mandate"           # mandate | discovery | research | selecting | monitoring | idle | error
        self.mandate = None
        self.engine = None
        self.ledger: Ledger | None = None
        self.candidates: list[dict] = []
        self.research: list[dict] = []
        self.task: asyncio.Task | None = None
        self.topup: dict | None = None
        self._topup_fut: asyncio.Future | None = None
        self.offers: list[dict] = []     # the selection desk: every coin that passed research this round, numbered from 1
        self.majors: list[dict] = []     # allowlisted majors (separate desk section, numbers 101+)
        self.round_no = 0
        self.versions = 0                # mandate versions used this session
        self._price_tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()      # background work started by fast-returning tools
        self._building: dict[int, float] = {}    # desk coin number -> amount whose card is being built right now
        self._build_tasks: set[asyncio.Task] = set()
        self._stop_requested = False
        self._exiting = False
        self.focus_coin: int | None = None       # the coin the user is talking about (last stop question / single pick)
        self._order_extras: dict[int, dict] = {}  # desk coin number -> {"stop": {...}, "risk_usdc": x} for the card being built

    # ---------- default dependencies (replaced in tests) ----------
    def _default_engine(self, m):
        from backend.execute.engine import TradingEngine
        e = TradingEngine(m, bus=self.bus, jev=self._jev_factory(), ui_button_enabled=self.ui_buttons,
                          log_path=self.runs / f"m6_trades_{m.mandate_id}.json")
        e.start_sweeper()
        e.listener.start()
        return e

    @staticmethod
    def _default_jev():
        from backend.judge.jev import JEVClient
        return JEVClient()

    async def _smart_money(self) -> tuple[dict, dict]:
        """Nansen smart-money net flow: one x402 call in live research mode (counted in the session's research ledger),
        the recorded response in rehearsal. Empty = fall back to DexScreener."""
        from backend.discovery.smart_money import smart_money_flows
        if os.environ.get("VIBE_SMART_MONEY", "1") == "0":
            return {}, {"source": "dexscreener"}
        if self.research_mode == "live" and self.ledger is not None:
            payer = self._payer_factory(self.ledger)
            try:
                return await smart_money_flows(payer)
            finally:
                await payer.close()
        return await smart_money_flows(CacheOnlyPayer(), ttl_s=10**9)

    async def _default_discover(self, m) -> list[dict]:
        flows, info = await self._smart_money()
        self.discovery_info = {**{k: v for k, v in info.items() if k != "error"}, "smart_money_tokens": len(flows),
                               "fallback": not flows}
        if self.discovery_mode == "replay":
            files = sorted(RUNS.glob("m3_*.json"))
            if not files:
                raise RuntimeError("replay mode found no runs/m3_*.json")
            coins = refilter_replay(json.loads(files[-1].read_text(encoding="utf-8"))["coins"], m)
        else:
            from backend.discovery.pipeline import run_pipeline
            from backend.discovery.smart_money import candidates_from
            cands = candidates_from(flows) if flows else None       # DexScreener when smart money is unavailable
            coins = [asdict(r) for r in await run_pipeline(m, log=lambda _s: None, candidates=cands)]
        for c in coins:                                             # numbers only: net flow and wallet count
            f = flows.get(c.get("mint"))
            if f:
                c.setdefault("metrics", {}).update({k: v for k, v in f.items() if v is not None})
                c["sources"] = sorted(set(c.get("sources") or []) | {"nansen_smart_money"})
        return coins

    @staticmethod
    async def _default_market(pairs: list[str]) -> dict[str, dict]:
        """Fresh DexScreener numbers for the desk (free API; also used in rehearsal). Failure = keep research-time numbers."""
        from backend.discovery.dexscreener import DexScreener, pair_metrics
        ds = DexScreener()
        try:
            got = await ds.pairs(pairs)
        finally:
            await ds.close()
        return {a: pair_metrics(p) for a, p in got.items()}

    def _default_payer(self, ledger):
        if self.research_mode == "live":
            from backend.research.x402_client import ResearchPayer
            return ResearchPayer(ledger, emit=self._emit_research)
        cache.TTL_S = 10**9      # replay: the cache never expires
        return CacheOnlyPayer()

    async def _default_research(self, coins, m, payer, ledger, approver, emit):
        from backend.research.pipeline import run_research
        from backend.solana_utils import Rpc
        rpc = Rpc(settings.solana_rpc_url or mainnet_rpc_url())
        try:
            res = await run_research(coins, m, rpc, payer, ledger, self._jev_factory(), approver, emit, log=lambda _s: None)
        finally:
            await rpc.close()
        return [asdict(r) for r in res]

    # ---------- event adapter ----------
    def _emit_research(self, type_: str, d: dict) -> None:
        d = dict(d)
        speak = d.pop("speak", "")
        if type_ == "research_payment":
            d["seller"] = safe_reason(d.get("seller", ""), 30)   # seller names come from our own registry, not from seller responses
            d["reason"] = safe_reason(d.get("reason", ""), 40)
            speak = ""   # payments go to the spend list only, not broadcast one by one (summarised at the end)
        if type_ == "research_topup_request":
            d["expires_at"] = time.time() + TOPUP_TIMEOUT_S
        self.bus.emit(type_, speak, d)

    # ---------- session start / new rounds ----------
    def start(self, m) -> None:
        """First freeze: create the engine and run the first research round."""
        if self.task and not self.task.done():
            raise RuntimeError("the pipeline is already running")
        self.mandate = m
        self.versions = 1
        self.bus.emit("mandate_frozen",
                      f"Mandate frozen, ID {m.mandate_id[-6:]}. Looking for coins now.",
                      {"mandate_id": m.mandate_id, "hash": m.hash, "expires_at": m.expires_at,
                       "body": m.body.model_dump(mode="json")})
        self.task = asyncio.create_task(self._run(m))

    def amend_block_reason(self) -> str | None:
        """Changing the rules is allowed unless a swap is executing (seconds). A running research round does not block:
        applying the change stops it (no new payment starts) and starts the new round. Pending cards are voided."""
        if self.mandate is None:
            return None
        if self.engine is not None and self.engine.in_flight():
            return "a trade is executing right now; try again in a few seconds"
        if self._exiting:
            return "the emergency exit is still selling; try again in a few seconds"
        return None

    def round_running(self) -> bool:
        return bool(self.task and not self.task.done())

    def void_for_amend(self) -> list[str]:
        """The rules are changing: pending cards (and cards still being built) must never execute under the old rules."""
        for t in list(self._build_tasks):
            t.cancel()
        self._build_tasks.clear()
        self._building.clear()
        for o in self.offers:
            o.pop("building", None)
        if self.engine is None:
            return []
        return self.engine.void_pending("the settings are changing")

    async def stop_round(self, why: str = "user", wait_s: float = 30.0) -> bool:
        """Stop the running research round. Discovery / cache-replay research are cancelled at once; live paid research
        refuses every new payment and finishes the one in flight (so the ledger stays exact). Nothing from the round
        reaches the desk. Returns False if no round was running."""
        if not self.round_running():
            return False
        self._stop_requested = True
        if self._topup_fut and not self._topup_fut.done():
            self._topup_fut.set_result(0.0)       # an open top-up question counts as "no"
        if self.phase == "discovery" or self.research_mode != "live":
            self.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), wait_s)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001  _run reports its own errors
            pass
        if not self.task.done():
            self.task.cancel()
        self.bus.emit("research_stopped",
                      "Research round stopped; nothing from it goes on the desk." if why == "user" else "",
                      {"round": self.round_no, "why": why})
        return True

    async def new_round(self, m, kind: str = "none") -> None:
        """Next research round under mandate m (same or amended). Cash, positions, trades and the spend ledger stay.
        If a round is still running (the user changed filters mid-research), it is stopped first."""
        why = self.amend_block_reason()
        if why:
            raise RuntimeError(why)
        if self.round_running():
            await self.stop_round("replanned")
        if self.engine is None:
            self.start(m)
            return
        changed = m is not self.mandate and m.mandate_id != self.mandate.mandate_id
        if changed:
            self.engine.set_mandate(m)
            self.versions += 1
            self._sync_ledger(self.mandate, m)
        self.mandate = m
        self.candidates, self.research = [], []
        # continuous co-pilot: coins you still hold stay on the desk (and keep their numbers); the rest make room for the new hunt
        held = {p.mint for p in self._open_positions()}
        self.offers = [o for o in self.offers if o["mint"] in held]
        what = {"filters": "new coin filters", "guards": "updated guard rules"}.get(kind, "new settings")
        speak = (f"Settings updated, version {self.versions}. Looking for coins with the {what} now; open positions keep their own exit rules."
                 if changed else "Starting a new research round with the same settings.")
        self.bus.emit("mandate_amended" if changed else "round_started", speak,
                      {"mandate_id": m.mandate_id, "hash": m.hash, "expires_at": m.expires_at, "version": self.versions, "kind": kind,
                       "body": m.body.model_dump(mode="json"), "round": self.round_no + 1})
        self.task = asyncio.create_task(self._run(m))

    def apply_now(self, m, speak: str) -> None:
        """A change that only lowers the user's own limits: takes effect at once, the desk and positions stay, no new round."""
        if self.engine is not None and self.mandate is not None and m.mandate_id != self.mandate.mandate_id:
            self.engine.set_mandate(m)
            self.versions += 1
            self._sync_ledger(self.mandate, m)
        self.mandate = m
        self.bus.emit("mandate_amended", speak,
                      {"mandate_id": m.mandate_id, "hash": m.hash, "expires_at": m.expires_at, "version": self.versions,
                       "kind": "tighten", "body": m.body.model_dump(mode="json"), "round": self.round_no})

    def _sync_ledger(self, old, new) -> None:
        if self.ledger is None:
            return
        d = round((new.body.budget.research_usdc - old.body.budget.research_usdc) * ATOMIC)
        if d:
            self.ledger.budget_atomic = max(self.ledger.spent_atomic, self.ledger.budget_atomic + d)
        self.ledger.per_call_cap_atomic = round(new.body.budget.research_per_call_usdc * ATOMIC)

    async def _run(self, m) -> None:
        self._stop_requested = False
        try:
            if self.engine is None:
                self.engine = self._engine_factory(m)
            self.engine.note_override = self.note_override
            await self.ensure_majors()
            if self.ledger is None:                 # created first: smart-money discovery is paid from the same ledger
                self.ledger = Ledger.from_mandate(m, self.runs / f"research_spend_{m.mandate_id}.json")
            self.round_no += 1
            # 1. discovery + free checks
            self.phase = "discovery"
            self.bus.emit("pipeline_status", "", {"phase": self.phase, "round": self.round_no})
            coins = await self._discover(m)
            self.candidates = [self._coin_panel(c) for c in coins]
            di = getattr(self, "discovery_info", None) or {}
            src = "from smart-money buying" if di.get("smart_money_tokens") and not di.get("fallback") else ""
            self.bus.emit("candidates_found", f"Found {len(coins)} candidate coins{' ' + src if src else ''}; running free checks.",
                          {"count": len(coins), "coins": [{k: c[k] for k in ("mint", "symbol", "paid_promo")} for c in self.candidates],
                           "discovery": di})
            survivors = [c for c in coins if c.get("status") == "pending_research"]
            n_free_fail = sum(c.get("status") == "fail_free" for c in coins)
            n_sim_fail = sum(c.get("status") == "fail_sell_sim" for c in coins)
            n_top = min(len(survivors), m.body.budget.research_max_coins_per_round)
            self.bus.emit("free_check_result",
                          f"Free checks done: {len(survivors)} of {len(coins)} coins passed"
                          + (f"; paid research only for the top {n_top}." if len(survivors) > n_top else "; on to paid research.")
                          + " No coins to name yet.",
                          {"total": len(coins), "fail_free": n_free_fail, "fail_sell_sim": n_sim_fail,
                           "survivors": len(survivors), "coins": self.candidates})
            # rank the free-check survivors for free (smart-money net flow, buy pressure, holders, ...); paid research
            # only for the few that will be shown. Same in rehearsal, so the recorded receipts match the shown coins.
            lim = m.body.budget.research_max_coins_per_round
            if len(survivors) > lim:
                survivors = sorted(survivors, key=lambda c: rank_score({**(c.get("metrics") or {}),
                    "roundtrip_loss_pct": (c.get("sell_sim") or {}).get("roundtrip_loss_pct")}), reverse=True)[:lim]
            m_research = m
            # 2. paid research (one spend ledger for the whole session)
            self.phase = "research"
            self.bus.emit("pipeline_status", "", {"phase": self.phase, "research_mode": self.research_mode})
            if self.ledger is None:
                self.ledger = Ledger.from_mandate(m, self.runs / f"research_spend_{m.mandate_id}.json")
            payer = _StoppablePayer(self._payer_factory(self.ledger), lambda: self._stop_requested)
            try:
                res = await self._research(survivors, m_research, payer, self.ledger, self._approve_topup, self._emit_research)
            finally:
                await payer.close()
            if self._stop_requested:
                raise RoundStopped()
            self.research = [self._research_panel(r) for r in res]
            passed = [r for r in res if r.get("status") == "pass"]
            spent = self.ledger.spent_atomic / ATOMIC
            self.bus.emit("research_done",
                          (f"Research done: spent {spent:.2f} dollars in total so far, {len(passed)} of {len(res)} coins passed." if res else
                           f"No coin got past the free checks, so nothing was researched (spent {spent:.2f} dollars in total so far).")
                          + ("" if passed else " No coin passed this round; you can change the filters and run another round."),
                          {"spent_usdc": spent, "budget_usdc": self.ledger.budget_atomic / ATOMIC, "passed": len(passed),
                           "results": self.research, "round": self.round_no})
            # 3. desk: the top few qualified coins, each with a one-line reason, its research receipt and the JEV's
            #    most suspicious item; numbers continue from earlier rounds (earlier coins stay on the desk)
            self._desk_building_round = self.round_no
            by_mint = {c["mint"]: c for c in coins}
            have = {o["mint"] for o in self.offers}
            start = max([o["number"] for o in self.offers] + [p.coin_number or 0 for p in self._open_positions()], default=0)
            cands = [self._offer(0, by_mint[r["mint"]], r, m) for r in passed if r["mint"] in by_mint and r["mint"] not in have]
            await self._refresh_market(cands)
            best = sorted(cands, key=lambda o: rank_score(o["metrics"]), reverse=True)[:TOP_N]
            offers = [o for o in cands if o in best]          # the top few, in research order
            res_by_mint = {r["mint"]: r for r in self.research}
            reasons = desk_reasons([o["metrics"] for o in offers], self._filters())   # each with a number of its own on this desk
            for i, o in enumerate(offers):
                o["number"], o["id"] = start + i + 1, f"r{self.round_no}c{start + i + 1}"
                o["reason"] = reasons[i]
                o["receipt"] = (res_by_mint.get(o["mint"]) or {}).get("spend") or []
            await self._judge_offers(offers)
            offers += await self._redteam_offer(start + len(offers) + 1, m)
            self.offers = self.offers + offers
            self.phase = "selecting" if offers else self._resting_phase()
            self.bus.emit("pipeline_status", "", {"phase": self.phase})
            if any(not o.get("redteam") for o in offers):
                self._announce_desk([o for o in offers if not o.get("redteam")])
            for o in offers:
                if o.get("redteam"):
                    self.bus.emit("redteam_ready",
                                  f"Rehearsal: coin {o['number']} is a red-team test coin. Its token text tries to give orders "
                                  "to AI agents; that text is ignored and never reaches me.", {"coin_number": o["number"]})
        except (asyncio.CancelledError, RoundStopped):
            if not self._stop_requested:
                raise
            # stopped by the user or replaced by an amended round: discard this round's partial results
            self.candidates, self.research, self.offers = [], [], []
            self.phase = self._resting_phase()
            self.bus.emit("pipeline_status", "", {"phase": self.phase, "stopped": True})
        except Exception as e:  # noqa: BLE001
            self.phase = "error"
            log_traceback("research round", e)
            self.bus.emit("pipeline_status", "Research hit an error on my side and stopped. Nothing was bought.",
                          {"phase": "error", "error": type(e).__name__, "detail": safe_reason(e, 120)}, loud=True)

    def _watch(self, t: asyncio.Task, what: str, loud: bool = False) -> asyncio.Task:
        """Keep a background task alive and report an unexpected crash out loud (never silently)."""
        self._bg.add(t)

        def done(task: asyncio.Task) -> None:
            self._bg.discard(task)
            if task.cancelled():
                return
            ex = task.exception()
            if ex is not None:
                log_traceback(what, ex)
                self.bus.emit("task_failed", f"The {what} hit an error on my side and stopped. Check the screen.",
                              {"what": what, "error": type(ex).__name__, "detail": safe_reason(ex, 120)}, loud=True)
        t.add_done_callback(done)
        return t

    def working_on(self) -> list[str]:
        """What is running in the background right now (fixed English, numbers and codes only)."""
        out = []
        if self.round_running():
            out.append(f"research round {self.round_no}: " + ("discovery and free checks" if self.phase == "discovery" else "paid research"))
        if self._building:
            out.append("building cards for coin " + ", ".join(map(str, sorted(self._building))))
        if self.engine is not None:
            codes = [c.code for c in self.engine.in_flight()]
            if codes:
                out.append("executing card " + ", ".join(codes))
        if self._exiting:
            out.append("emergency exit: selling")
        return out

    def _resting_phase(self) -> str:
        if self.engine is not None and any(p.status in ("open", "stuck") for p in self.engine.positions.values()):
            return "monitoring"
        return "idle"

    def current_phase(self) -> str:
        """The stored phase, except that after a round it follows reality (never stuck in "monitoring" once all is sold)."""
        if self.phase in ("mandate", "discovery", "research", "error") or (self.task and not self.task.done()):
            return self.phase
        if any(o["status"] == "open" and o.get("kind") != "major" for o in self._desk()) or (self.engine is not None and
                                                                 any(c.status == "pending" for c in self.engine.cards.values())):
            return "selecting"
        return self._resting_phase()

    @staticmethod
    def _coin_panel(c: dict) -> dict:
        return {"mint": c["mint"], "symbol": sanitize_symbol(c.get("symbol_display", "")), "paid_promo": bool(c.get("paid_promo")),
                "status": c.get("status"), "status_label": STATUS_LABEL.get(c.get("status"), c.get("status")),
                "failed_stage": c.get("failed_stage"), "metrics": _nums(c.get("metrics") or {}),
                "pool": _pool_of(c),
                "roundtrip_loss_pct": (c.get("sell_sim") or {}).get("roundtrip_loss_pct"),
                "reasons": [safe_reason(x) for x in (c.get("reasons") or [])[:4]]}

    @staticmethod
    def _research_panel(r: dict) -> dict:
        f = r.get("facts") or {}
        return {"mint": r["mint"], "symbol": sanitize_symbol(r.get("symbol_display", "")), "status": r.get("status"),
                "status_label": STATUS_LABEL.get(r.get("status"), r.get("status")),
                "facts": {k: f.get(k) for k in ("holders", "top10_pct", "dev_pct", "known_rugger") if k in f},
                "spend": [{"seller": SELLER_LABEL.get(s.get("seller"), "?"), "cached": bool(s.get("cached")),
                           "amount_usdc": s.get("amount_usdc", 0.0), "tx_hash": s.get("tx_hash"),
                           "recorded_usdc": s.get("recorded_usdc")} for s in r.get("spend") or []],
                "reasons": [safe_reason(x) for x in (r.get("reasons") or [])[:4]]}

    # ---------- selection desk ----------
    def _offer(self, number: int, c: dict, r: dict, m) -> dict:
        met = c.get("metrics") or {}
        f = r.get("facts") or {}
        lp = c.get("lp") or {}
        flt = m.body.filters
        d = {k: met.get(k) for k in ("liquidity_usd", "market_cap", "volume_h24", "pair_created_at_ms", "price_usd",
                                     "price_change_h1", "price_change_h6", "price_change_h24", "volume_h1", "volume_h6",
                                     "buys_h1", "sells_h1", "buys_h24", "sells_h24", "sm_netflow_24h_usd", "sm_netflow_1h_usd",
                                     "sm_traders")}
        d.update({"holders": f.get("holders"), "top10_pct": f.get("top10_pct"), "dev_pct": f.get("dev_pct"),
                  "roundtrip_loss_pct": (c.get("sell_sim") or {}).get("roundtrip_loss_pct"),
                  "lp_burned_pct": lp.get("lp_burned_pct"), "lp_locked_pct": lp.get("lp_locked_pct")})
        # authorities: the free checks fail any coin whose mint / freeze authority is still live when the filter requires it
        d["mint_revoked"] = met.get("mint_revoked", True if flt.require_mint_revoked else None)
        d["freeze_revoked"] = met.get("freeze_revoked", True if flt.require_freeze_revoked else None)
        d["known_rugger"] = f.get("known_rugger")
        return {"id": f"r{self.round_no}c{number}", "number": number, "round": self.round_no, "mint": c["mint"],
                "symbol": sanitize_symbol(c.get("symbol_display", "")), "pool": _pool_of(c), "dex": _safe_dex(met.get("dex")),
                "paid_promo": bool(c.get("paid_promo")), "metrics": _clean(d), "status": "open", "card_ids": [],
                "market_live": False, "market_at": None}

    async def _refresh_market(self, offers: list[dict]) -> None:
        pools = [o["pool"] for o in offers if o.get("pool")]
        if not pools:
            return
        try:
            fresh = await asyncio.wait_for(self._market(pools), 15)
        except Exception:  # noqa: BLE001  the desk still works with research-time numbers
            return
        for o in offers:
            f = fresh.get(o.get("pool") or "")
            if f:
                o["metrics"].update(_clean(f))
                o["metrics"].update(_clean(o["metrics"]))          # re-derive volume/liquidity from the merged numbers
                o["market_live"], o["market_at"] = True, time.time()

    async def refresh_market(self) -> dict:
        await self.ensure_majors()          # majors refresh in the background (single flight), never inline
        await self._refresh_market(self.offers)
        live = sum(1 for o in self.offers if o.get("market_live")) + sum(1 for o in self.majors if o.get("market_live"))
        return {"ok": True, "live": live}

    def _desk(self) -> list[dict]:
        """Offers with their status derived from their latest card (a rejected / expired card puts the coin back on the desk)."""
        out = []
        cards = self.engine.cards if self.engine is not None else {}
        for o in list(self.offers) + list(self.majors):
            card = None
            if o["card_ids"]:
                card = cards.get(o["card_ids"][-1])
                while card is not None and card.replaced_by and card.replaced_by in cards:
                    card = cards[card.replaced_by]
            st = o["status"]
            if o.get("building"):
                st = "building"
            elif card is not None and st != "skipped":
                st = {"pending": "carded", "approved": "executing", "executed": "bought"}.get(card.status, "open")
            out.append({**o, "status": st, "card": ({"id": card.id, "code": card.code, "status": card.status,
                                                     "amount_usdc": card.amount_usdc} if card else None)})
        return out

    def _offer_public(self, o: dict) -> dict:
        m = o["metrics"]
        return {"id": o["id"], "number": o["number"], "round": o["round"], "mint": o["mint"], "symbol": o["symbol"],
                "pool": o["pool"], "dex": o["dex"], "paid_promo": o["paid_promo"], "metrics": m, "status": o["status"],
                "card": o.get("card"), "market_live": o["market_live"], "market_at": o["market_at"],
                "kind": o.get("kind") or "meme", "ticker": o.get("ticker"),
                "reason": o.get("reason"), "receipt": o.get("receipt") or [], "suspicion": o.get("suspicion"),
                "warning": o.get("warning"), "redteam": bool(o.get("redteam")),
                "stop": o.get("stop"),
                "signals": _with_injection_flag(desk_signals(m, o["paid_promo"], self.mandate.body.filters
                                                             if o.get("kind") != "major" and self.mandate else None), o)}

    async def _redteam_offer(self, number: int, m) -> list[dict]:
        """Rehearsal only: a coin whose token text carries a prompt injection. The text is flagged by code and kept away
        from the agent; buying it shows the guard blocking the transfer a naive builder would have added."""
        from backend.copilot import redteam
        from backend.config import is_mainnet
        if (is_mainnet() or not redteam.enabled() or self.discovery_mode == "live" or self.research_mode == "live"
                or self.engine is None):
            return []
        fx = redteam.load()
        if any(o["mint"] == fx["mint"] for o in self.offers) or not hasattr(self.engine, "redteam"):
            return []
        # the token's own name / symbol are part of the attack text: never shown, only a fixed label
        c = {"mint": fx["mint"], "symbol_display": "TESTCOIN", "metrics": {"pair": fx["pool"], "dex": fx["dex"]}}
        o = self._offer(number, c, {"facts": {}}, m)
        await self._refresh_market([o])
        o.update({"reason": "red-team test coin (rehearsal only)", "receipt": [], "redteam": True,
                  "injection": redteam.has_injection(fx["metadata"])})
        self.engine.redteam[fx["mint"]] = fx["attacker"]
        try:    # fork cheat code: give the made-up attacker an empty USDC account, so the guard's simulation runs to the end
            from backend.config import USDC_MINT
            await self.engine.rpc.call("surfnet_setTokenAccount", [fx["attacker"], USDC_MINT, {"amount": 0}, redteam.TOKEN_PROGRAM])
        except Exception:  # noqa: BLE001
            pass
        return [o]

    async def _judge_offers(self, offers: list[dict]) -> None:
        """JEV: two or three specific numeric yes/no questions per recommended coin; P(true) is the suspicion score."""
        if not offers:
            return
        try:
            jev = self._jev_factory()
        except Exception:  # noqa: BLE001
            return
        got = await asyncio.gather(*(suspicion(jev, o["metrics"]) for o in offers), return_exceptions=True)
        for o, g in zip(offers, got):
            o["suspicion"] = g if isinstance(g, dict) else None
            # a worry is shown (and asked about) only as a concrete number of this coin
            sus = o["suspicion"]
            o["warning"] = (warning_text(sus.get("key"), o["metrics"], self._filters())
                            if sus and (sus.get("score") or 0) >= SUSPICION_ASK else None)

    def _filters(self):
        """The mandate's coin filters (their limits decide what counts as near a limit); None = schema defaults."""
        return self.mandate.body.filters if self.mandate is not None else None

    def _announce_desk(self, offers: list[dict] | None = None) -> None:
        offers = offers if offers is not None else self.offers
        n = len(offers)
        cap, left = self.buy_cap_usdc(), self.remaining_usdc()
        words = {1: "One coin looks", 2: "Two coins look", 3: "Three coins look"}.get(n, f"{n} coins look")
        lines = []
        # one question at most: only the single most suspicious coin, and only when that item is likely enough to change the pick
        score = lambda o: (o.get("suspicion") or {}).get("score") or 0 if o.get("warning") else 0  # noqa: E731
        worst = max(offers, key=score) if offers else None
        for o in offers:
            # the same line as on screen, in spoken words (rounded numbers, nothing added)
            line = f"Coin {o['number']}: {spoken(o.get('reason') or 'Passed every safety check')}."
            if o is worst and score(o) >= SUSPICION_ASK:
                # the figure and the limit it is near or over, never a label ("concentration worry")
                line += f" {spoken(o['warning'])}. Still want it on the list?"
            lines.append(line)
        if len(offers) == 1:
            self.focus_coin = offers[0]["number"]
        self.bus.emit("desk_ready",
                      f"{words} tradeable. " + " ".join(lines)
                      + " Ask me where the stop should be, or tell me how much you're willing to lose on one.",
                      {"count": n, "cap_usdc": cap, "remaining_usdc": left, "round": self.round_no,
                       "numbers": [o["number"] for o in offers]})

    def remaining_usdc(self) -> float:
        if self.mandate is None or self.engine is None:
            return 0.0
        pending = sum(c.amount_usdc for c in self.engine.cards.values() if c.status in ("pending", "approved"))
        pending += sum(self._building.values())        # cards still being simulated hold their budget too
        return round(max(0.0, self.mandate.body.budget.total_usdc - self.engine.spent_usdc - pending), 6)

    def buy_cap_usdc(self) -> float:
        return round(min(self.mandate.body.budget.per_trade_usdc, self.remaining_usdc()), 6) if self.mandate else 0.0

    async def place_orders(self, orders: dict[int, float] | None, utterance: str | None, channel: str, wait: bool = False) -> dict:
        """Create one confirmation card per picked coin. Voice: coins and amounts are extracted from the user's
        own latest transcript (deterministic parse, then the mandate LLM path if needed). Agent-provided numbers
        are ignored so a bad parse cannot reject a correct order. Limits: per-trade cap per coin, remaining
        session budget for the sum. Validation is synchronous; the cards are built in the background unless
        wait=True, and their codes arrive in the "cards_ready" update."""
        # Research is done and the desk is only loading live prices: wait for it instead of refusing the order.
        if self.round_running() and getattr(self, "_desk_building_round", None) == self.round_no:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 30.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        await self.ensure_majors()
        desk = {o["number"]: o for o in self._desk()}
        open_nums = sorted(n for n, o in desk.items() if o["status"] in ("open", "skipped"))
        tickers = {o["ticker"]: o["number"] for o in desk.values() if o.get("ticker") and o["status"] in ("open", "skipped")}
        if not open_nums:
            return {"ok": False, "reason": "no coin on the desk is waiting for an order"}
        cap, left = self.buy_cap_usdc(), self.remaining_usdc()
        per_trade = self.mandate.body.budget.per_trade_usdc
        bb = getattr(self.engine, "buy_block_reason", None)
        block = bb() if callable(bb) else None
        if block:
            return {"ok": False, "reason": block, "speak": block[:1].upper() + block[1:] + ". Selling is still allowed."}
        risk, risk_coin = parse_risk(utterance) if channel == "voice" else (None, None)
        if risk is not None:
            return await self._risk_order(risk, risk_coin, open_nums, cap, left, per_trade, wait)
        if channel == "voice":
            heard, why = await resolve_desk_order(utterance, open_nums, cap, tickers=tickers)
            orders = heard
        elif channel == "button":
            why = None if self.ui_buttons else "the on-screen buttons are not enabled"
        else:
            why = "unknown channel"
        if why is None:
            orders = {int(k): round(float(v), 6) for k, v in (orders or {}).items()}
            bad = [k for k in orders if k not in open_nums]
            if not orders:
                why = "no coins were picked"
            elif bad:
                why = f"coin {bad[0]} is not open on the desk (open: {', '.join(map(str, open_nums))})"
            elif any(v <= 0 for v in orders.values()):
                why = "every amount must be more than 0"
            elif any(v > per_trade + 1e-9 for v in orders.values()):
                k = next(k for k, v in orders.items() if v > per_trade + 1e-9)
                why = f"{orders[k]:g} dollars for coin {k} is above your per-trade cap of {per_trade:g} dollars"
            elif sum(orders.values()) > left + 1e-9:
                why = f"{sum(orders.values()):g} dollars in total is more than the {left:g} dollars left in the session"
        if why:
            return {"ok": False, "reason": why,
                    "speak": f"I can't place that ({why}). Max ${cap:g} per coin, ${left:g} left in the session."}
        by_num = {o["number"]: o for o in (self.offers + self.majors)}
        for n, v in orders.items():
            self._building[n] = v
            by_num[n]["building"] = True
        if wait:
            return await self._build_cards(orders)
        t = asyncio.create_task(self._build_cards(orders))
        self._build_tasks.add(t)
        t.add_done_callback(self._build_tasks.discard)
        self._watch(t, "card building")
        self.bus.emit("cards_building", "", {"orders": [{"coin_number": n, "amount_usdc": orders[n]} for n in sorted(orders)]})
        return {"ok": True, "status": "building_cards",
                "orders": [{"coin_number": n, "amount_usdc": orders[n]} for n in sorted(orders)],
                "note": "the cards are being simulated in the background; their codes arrive as a system update"}

    def _coin_from(self, coin: int | None, open_nums: list[int]) -> int | None:
        if coin is not None:
            return coin
        if self.focus_coin in open_nums:
            return self.focus_coin
        return open_nums[0] if len(open_nums) == 1 else None

    async def suggest_stop(self, coin: int | None, utterance: str | None) -> dict:
        """'Where should my stop be?' -> the coin's volatility stop (5-25%), in one sentence; remembers the coin."""
        from backend.copilot.desk import COIN_RE
        from backend.execute.cards import normalize_utterance
        from backend.market.volatility import stop_words
        if coin is None and utterance:
            mm = COIN_RE.search(normalize_utterance(utterance))
            coin = int(mm[1]) if mm else None
        desk = {o["number"]: o for o in self.offers}
        pos = self._find_position(coin) if coin is not None else (self._open_positions()[0] if len(self._open_positions()) == 1 and not desk else None)
        n = self._coin_from(coin, sorted(n for n, o in desk.items() if o["status"] != "skipped")) if pos is None else pos.coin_number
        o = desk.get(n) if n is not None else None
        if o is None and pos is None:
            return {"ok": False, "reason": "which coin?", "speak": "Which coin? Say its number."}
        pool = (o or {}).get("pool") or getattr(pos, "pool", None)
        st = o.get("stop") if o is not None and o.get("stop") else None
        if st is None:
            st = await (self.engine.stop_for(pool) if self.engine is not None else _vol_stop(pool))
            if o is not None:
                o["stop"] = st
        self.focus_coin = n
        speak = stop_words(st, f"coin {n}")
        self.bus.emit("stop_suggested", "", {"coin_number": n, **{k: st.get(k) for k in ("stop_pct", "atr_pct", "basis", "tf")}})
        return {"ok": True, "coin_number": n, "stop_pct": st.get("stop_pct"), "atr_pct": st.get("atr_pct"),
                "basis": st.get("basis"), "speak": speak}

    async def _risk_order(self, risk: float, coin: int | None, open_nums: list[int], cap: float, left: float,
                          per_trade: float, wait: bool) -> dict:
        """'I'm willing to lose one dollar on this' -> amount = risk / stop distance, capped by the per-trade cap and
        what is left; the card shows the stop and the risk."""
        n = self._coin_from(coin, open_nums)
        if n is None or n not in open_nums:
            return {"ok": False, "reason": "which coin?", "speak": "Which coin? Say its number, for example lose one dollar on coin 1."}
        o = {x["number"]: x for x in self.offers}.get(n)
        if o is None:
            return {"ok": False, "reason": f"coin {n} is not on the desk"}
        st = o.get("stop") or await self.engine.stop_for(o.get("pool"))
        o["stop"] = st
        amt, note = size_from_risk(risk, float(st["stop_pct"]), per_trade, left)
        if amt <= 0:
            return {"ok": False, "reason": "nothing left to spend", "speak": "There is no money left to spend today."}
        self.focus_coin = n
        self._order_extras[n] = {"stop": st, "risk_usdc": round(min(risk, amt * st["stop_pct"] / 100), 4),
                                 "cap_note": CAP_WORDS.get(note) if note else None}
        self._building[n] = amt
        o["building"] = True
        if wait:
            return await self._build_cards({n: amt})
        t = asyncio.create_task(self._build_cards({n: amt}))
        self._build_tasks.add(t)
        t.add_done_callback(self._build_tasks.discard)
        self._watch(t, "card building")
        self.bus.emit("cards_building", "", {"orders": [{"coin_number": n, "amount_usdc": amt}]})
        return {"ok": True, "status": "building_cards", "orders": [{"coin_number": n, "amount_usdc": amt}],
                "stop_pct": st["stop_pct"], "note": "sized from the risk; the card code arrives as a system update"}

    async def _build_cards(self, orders: dict[int, float]) -> dict:
        made, failed = [], []
        by_num = {o["number"]: o for o in (self.offers + self.majors)}
        mandate_id = self.mandate.mandate_id
        try:
            for n in sorted(orders):
                o = by_num.get(n)
                if o is None:
                    failed.append(n)
                    continue
                extra = self._order_extras.pop(n, {})
                try:
                    card = await self.engine.create_card(o["mint"], orders[n], _card_metrics(o["metrics"]), o["symbol"], o.get("pool"),
                                                         coin_number=n if o.get("kind") != "major" else n, announce=False,
                                                         stop=extra.get("stop") or o.get("stop"), risk_usdc=extra.get("risk_usdc"),
                                                         cap_note=extra.get("cap_note"))
                    if card is not None and o.get("suspicion") and o.get("warning"):
                        card.suspicion = {k: o["suspicion"].get(k) for k in ("label", "score")}
                    if card is not None and o.get("warning"):
                        card.warning = o["warning"]
                    if card is not None and o.get("ticker"):
                        card.ticker = o["ticker"]
                finally:
                    self._building.pop(n, None)
                    o.pop("building", None)
                if card is None:
                    failed.append(n)
                    continue
                if self.mandate is None or self.mandate.mandate_id != mandate_id:
                    # the rules changed while this card was simulated: it never executes under the old ones
                    self.engine.void_pending("the settings changed while it was being built")
                    failed.append(n)
                    continue
                o["status"] = "open"
                o["card_ids"].append(card.id)
                made.append(card)
        finally:
            for n in orders:
                self._building.pop(n, None)
                if n in by_num:
                    by_num[n].pop("building", None)
        if made:
            # majors are named by their fixed ticker (JUP), meme coins by desk number only (coin 1)
            parts = [f"card {c.code}: {c.ticker or ('coin ' + str(c.coin_number))}, {_spoken_amount(c.amount_usdc)}"
                     + (f", {c.cap_note}" if getattr(c, "cap_note", None) else "")
                     + (f", stop {round(c.stop_loss_pct)} percent" if c.stop_loss_pct is not None else "")
                     + (f": plan to lose about {_loss_words(c.loss_plan_usdc)}, worst case about {_loss_words(c.loss_worst_usdc)}"
                        if getattr(c, "loss_plan_usdc", None) is not None and getattr(c, "loss_worst_usdc", None) is not None else "")
                     + (", guard flagged it for your attention" if c.guard.get("decision") == "ask_human" else "")
                     for c in made]
            timeout = self.mandate.body.exec.approval_timeout_sec
            joined = "; ".join(parts)
            self.bus.emit("cards_ready",
                          joined[:1].upper() + joined[1:] + ". Details are on screen. "
                          f"Approve each one by its code, for example \"approve {made[0].code}\". No answer in {timeout} seconds counts as a reject."
                          + (f" No card for coin {', '.join(map(str, failed))} (see the screen)." if failed else ""),
                          {"cards": [c.public() for c in made], "failed": failed})
        elif failed:
            self.bus.emit("cards_ready", f"No card could be built for coin {', '.join(map(str, failed))}; see the screen for why.",
                          {"cards": [], "failed": failed})
        return {"ok": bool(made), "status": "cards_created" if made else "no_cards",
                "cards": [{"coin_number": c.coin_number, "code": c.code, "amount_usdc": c.amount_usdc,
                           "guard": c.guard.get("decision")} for c in made], "failed_coins": failed}

    def skip_coins(self, numbers: list[int] | None, utterance: str | None, channel: str) -> dict:
        desk = {o["number"]: o for o in self._desk()}
        open_nums = sorted(n for n, o in desk.items() if o["status"] == "open")
        if channel == "voice":
            numbers, why = parse_desk_skip(utterance, open_nums)
            if why or not numbers:
                return {"ok": False, "reason": why or "no open coin matched"}
        numbers = [n for n in (numbers or []) if n in open_nums]
        if not numbers:
            return {"ok": False, "reason": "no open coin matched"}
        for o in self.offers:
            if o["number"] in numbers:
                o["status"] = "skipped"
        self.bus.emit("coins_skipped", f"OK, skipping coin{'s' if len(numbers) > 1 else ''} {', '.join(map(str, numbers))}.",
                      {"numbers": numbers})
        return {"ok": True, "status": "skipped", "numbers": numbers}

    # ---------- research top-ups ----------
    async def _approve_topup(self, req: dict) -> float | None:
        fut = asyncio.get_running_loop().create_future()
        self._topup_fut = fut
        self.topup = {"needed_usdc": req["needed_usdc"], "spent_usdc": req["spent_usdc"], "budget_usdc": req["budget_usdc"],
                      "suggest_usdc": req["suggest_usdc"], "expires_at": time.time() + TOPUP_TIMEOUT_S}
        try:
            return await fut
        finally:
            self.topup, self._topup_fut = None, None

    def respond_topup(self, action: str, amount_usdc: float | None, utterance: str | None, channel: str) -> dict:
        if self._topup_fut is None or self._topup_fut.done():
            return {"ok": False, "reason": "there is no pending top-up request"}
        if action == "reject":
            self._topup_fut.set_result(0.0)
            return {"ok": True, "outcome": "rejected"}
        amount = float(amount_usdc or 0)
        if amount <= 0:
            return {"ok": False, "reason": "the top-up amount must be greater than 0"}
        if channel == "voice":
            why = verify_topup(utterance, amount)
        elif channel == "button":
            why = None if self.ui_buttons else "the on-screen buttons are not enabled"
        else:
            why = "unknown channel"
        if why:
            self.bus.emit("voice_rejected", f"I could not confirm the top-up ({why}). To add budget, say \"add\" with the amount, for example \"add 0.5 dollars\".",
                          {"target": "topup", "why": why})
            return {"ok": False, "reason": why}
        self._topup_fut.set_result(amount)
        return {"ok": True, "outcome": "approved", "amount_usdc": amount}

    # ---------- emergency exit ----------
    async def emergency_exit(self, utterance: str | None, channel: str, wait: bool = False) -> dict:
        if self.engine is None:
            return {"ok": False, "reason": "no trading engine yet (the mandate is not frozen)"}
        if channel == "voice":
            why = verify_sell_all(utterance)
            if why:
                self.bus.emit("voice_rejected", f"I did not run the emergency exit ({why}). To exit, say clearly \"sell all\".", {"target": "emergency_exit", "why": why})
                return {"ok": False, "reason": why}
        n = sum(getattr(p, "status", "open") in ("open", "stuck") for p in self.engine.positions.values())
        if wait:
            return {"ok": True, "results": await self.engine.emergency_exit()}
        self._exiting = True

        async def run():
            try:
                await self.engine.emergency_exit()
            finally:
                self._exiting = False
        self._watch(asyncio.create_task(run()), "emergency exit", loud=True)
        return {"ok": True, "status": "selling", "positions": n,
                "note": "selling in the background; each fill arrives as a system update"}

    # ---------- rehearsal price test (fork only) ----------
    def price_test_enabled(self) -> bool:
        from backend.config import is_mainnet
        if is_mainnet() or os.environ.get("VIBE_PRICE_TEST", "1") == "0" or self.engine is None:
            return False
        if self.discovery_mode == "live" or self.research_mode == "live":
            return False      # rehearsal only
        lst = getattr(self.engine, "listener", None)
        if lst is not None and not getattr(lst, "fork_mode", False):
            return False
        try:
            from backend.execute.executor import assert_local
            assert_local(self.engine.rpc)
        except Exception:  # noqa: BLE001
            return False
        return True

    async def price_test(self, kind: str, utterance: str | None, channel: str) -> dict:
        if kind not in ("take_profit", "stop_loss"):
            return {"ok": False, "reason": "unknown test"}
        if not self.price_test_enabled():
            return {"ok": False, "reason": "price tests only run in rehearsal mode on the local fork"}
        if channel == "voice":
            why = verify_price_test(utterance, kind)
            if why:
                return {"ok": False, "reason": why}
        elif channel != "button":
            return {"ok": False, "reason": "unknown channel"}
        open_pos = [p for p in self.engine.positions.values() if p.status == "open" and p.held_tokens > 0]
        if not open_pos:
            return {"ok": False, "reason": "there is no open position to test"}
        if any(not x.done() for x in self._price_tasks):
            # two whales on one pool overshoot each other and either test can end on the other's sell
            return {"ok": False, "reason": "a price test is already running; its result arrives as a system update"}
        self._price_tasks = [x for x in self._price_tasks if not x.done()]
        t = asyncio.create_task(self._price_test(self.engine, open_pos[0], kind))
        self._price_tasks.append(t)
        return {"ok": True, "status": "started", "note": "the listener sells as usual; the result arrives as a system update"}

    @staticmethod
    async def _default_price_test(engine, pos, kind):
        from backend.execute.price_test import run_price_test
        return await run_price_test(engine, pos, kind)


    # ---------- majors + position management ----------
    MAJORS_REFRESH_S = 15.0

    async def ensure_majors(self) -> None:
        if not self.majors_enabled:
            return
        """Load the allowlisted majors onto their own desk section (no research spend).
        Templates appear immediately; live prices / pools refresh in the background so
        a research round or a desk refresh never waits on the majors feed. At most one
        majors refresh runs at a time (the desk polls every few seconds; overlapping
        refreshes used to queue up behind the price-API rate limiter and hang /desk/refresh)."""
        if not self.majors:
            self.majors = all_templates()
        running = getattr(self, "_majors_task", None)
        if running is not None and not running.done():
            return
        if time.time() - getattr(self, "_majors_at", 0.0) < self.MAJORS_REFRESH_S:
            return
        self._majors_at = time.time()
        t = asyncio.create_task(self._refresh_majors())
        self._majors_task = t
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _refresh_majors(self) -> None:
        if not self.majors:
            return
        try:
            prices = await asyncio.wait_for(live_prices([o["mint"] for o in self.majors]), 20)
        except Exception:  # noqa: BLE001
            prices = {}
        now = time.time()
        for o in self.majors:
            if not o.get("pool"):
                try:
                    o["pool"] = await asyncio.wait_for(find_pool(o["mint"]), 15)
                except Exception:  # noqa: BLE001
                    pass
            px = prices.get(o["mint"])
            if px:
                o["metrics"] = {**(o.get("metrics") or {}), "price_usd": px}
                o["market_live"], o["market_at"] = True, now

    def _open_positions(self) -> list:
        if self.engine is None:
            return []
        return [p for p in self.engine.positions.values() if p.status == "open"]

    def _position_numbers(self) -> list[int]:
        nums = []
        for p in self._open_positions():
            if p.coin_number is not None:
                nums.append(p.coin_number)
        return sorted(set(nums))

    def _position_tickers(self) -> dict[str, int]:
        out = {}
        for p in self._open_positions():
            if p.ticker and p.coin_number is not None:
                out[p.ticker] = p.coin_number
            elif p.ticker:
                # majors bought from the desk keep their 101+ number on the position
                pass
        return out

    def _find_position(self, coin: int | None, ticker: str | None = None):
        for p in self._open_positions():
            if ticker and p.ticker and p.ticker.upper() == ticker.upper():
                return p
            if coin is not None and p.coin_number == coin:
                return p
        return None

    def note_override(self, detail: dict) -> None:
        hook = getattr(self, "override_hook", None)
        if callable(hook):
            hook(detail)

    async def manage_position(self, utterance: str | None, channel: str, wait: bool = False) -> dict:
        """Create a confirmation card for a sell / close / stop / take-profit change extracted from the words."""
        if self.engine is None:
            return {"ok": False, "reason": "no open positions yet", "speak": "There is no open position to change."}
        self.engine.note_override = self.note_override
        opens = self._open_positions()
        if not opens:
            return {"ok": False, "reason": "no open positions", "speak": "There is no open position to change."}
        nums = self._position_numbers()
        # include 101+ majors numbers stored on the position
        for p in opens:
            if p.coin_number is not None and p.coin_number not in nums:
                nums.append(p.coin_number)
        tickers = {p.ticker: p.coin_number for p in opens if p.ticker and p.coin_number is not None}
        if channel == "voice":
            action, why = await resolve_position_action(utterance, nums, tickers)
        elif channel == "button":
            return {"ok": False, "reason": "use voice or text for position actions"}
        else:
            action, why = None, "unknown channel"
        if action is None:
            return {"ok": False, "reason": why, "speak": "I can't do that (" + (why or "unclear") + ")."}
        targets = list(opens) if action.coins == "all" else []
        if action.coins != "all":
            for n in action.coins:
                p = self._find_position(n, action.ticker)
                if p is None:
                    return {"ok": False, "reason": "coin " + str(n) + " is not an open position",
                            "speak": "Coin " + str(n) + " is not an open position."}
                targets.append(p)
        if not targets:
            return {"ok": False, "reason": "no matching position", "speak": "I could not find that position."}
        cards = []
        for pos in targets:
            if action.kind == "sell":
                c = self.engine.create_action_card(
                    "sell", pos.mint, coin_number=pos.coin_number, ticker=pos.ticker,
                    symbol=pos.symbol_display, sell_fraction=action.fraction, announce=False)
            else:
                adjust = {"break_even": bool(action.break_even)}
                if action.stop_loss_pct is not None:
                    adjust["stop_loss_pct"] = action.stop_loss_pct
                if action.take_profit_pct is not None:
                    adjust["take_profit_pct"] = action.take_profit_pct
                c = self.engine.create_action_card(
                    "adjust_exit", pos.mint, coin_number=pos.coin_number, ticker=pos.ticker,
                    symbol=pos.symbol_display, adjust=adjust, announce=False)
            cards.append(c)
        codes = [c.code for c in cards]
        if len(cards) == 1:
            speak = self.engine._action_card_speak(cards[0])
        else:
            speak = "Cards " + " and ".join(codes) + " are ready. Approve each by saying its code."
        self.bus.emit("cards_ready", speak, {"cards": [c.public() for c in cards], "kind": action.kind})
        return {"ok": True, "status": "cards_ready", "codes": codes, "speak": speak,
                "cards": [c.public() for c in cards]}

    # ---------- status ----------
    def status(self) -> dict:

        eng = self.engine.status() if self.engine is not None else None
        led = self.ledger
        why = self.amend_block_reason()
        return {"phase": self.current_phase(), "round": self.round_no, "mandate_versions": self.versions,
                "modes": {"discovery": self.discovery_mode, "research": self.research_mode, "ui_buttons": self.ui_buttons},
                "mandate": ({"mandate_id": self.mandate.mandate_id, "hash": self.mandate.hash, "expires_at": self.mandate.expires_at}
                            if self.mandate else None),
                "candidates": self.candidates, "research": self.research,
                "spend": ({"budget_usdc": led.budget_atomic / ATOMIC, "spent_usdc": led.spent_atomic / ATOMIC,
                           "entries": [{"seller": SELLER_LABEL.get(e.seller, "?"), "amount_usdc": e.amount_usdc, "status": e.status,
                                        "coin_mint": e.coin_mint, "reason": safe_reason(e.reason, 40), "tx_hash": e.tx_hash,
                                        "ts": e.ts} for e in led.entries],
                           "topups": [asdict(t) for t in led.topups]} if led else None),
                "topup": self.topup, "engine": eng,
                "desk": {"offers": [self._offer_public(o) for o in self._desk() if o.get("kind") != "major"],
                         "majors": [self._offer_public(o) for o in self._desk() if o.get("kind") == "major"],
                         "cap_usdc": self.buy_cap_usdc(),
                         "per_trade_usdc": self.mandate.body.budget.per_trade_usdc if self.mandate else None,
                         "remaining_usdc": self.remaining_usdc(), "round": self.round_no},
                "amend": {"allowed": self.mandate is not None and why is None, "blocked_reason": why},
                "working_on": self.working_on(), "round_running": self.round_running(),
                "price_test_enabled": self.price_test_enabled(), "network": network_status()}

    def agent_status(self) -> dict:
        """Status for the voice agent: only numbers, codes and our own fixed labels; no token names or third-party text."""
        ph = self.current_phase()
        s = {"phase": PHASE_AGENT_LABEL.get(ph, ph), "research_round": self.round_no, "working_on": self.working_on()}
        if self.ledger:
            s["research_spent_usd"] = round(self.ledger.spent_atomic / ATOMIC, 2)
            s["research_budget_usd"] = round(self.ledger.budget_atomic / ATOMIC, 2)
            s["research_payments"] = [{"seller": SELLER_LABEL.get(e.seller, "?"), "usd": e.amount_usdc} for e in self.ledger.entries
                                      if e.status in ("settled", "unknown")]
        desk = self._desk()
        if desk:
            s["selection_desk"] = {
                "max_per_coin_usd": self.buy_cap_usdc(), "session_left_usd": self.remaining_usdc(),
                "coins": [{"number": o["number"], "status": o["status"], **({"card_code": o["card"]["code"]} if o.get("card") else {}),
                           **({"why_recommended": o["reason"]} if o.get("reason") else {}),
                           # a model's suspicion reaches the agent only with a figure near or over a limit (the warning);
                           # the top-10 share always comes with what it is compared to
                           **({"warning": o["warning"], "most_suspicious": o["suspicion"]["label"], "suspicion": o["suspicion"]["score"]}
                              if o.get("suspicion") and o.get("warning") else {}),
                           **({"top10_vs_limit": note} if (note := holder_note(o["metrics"], [p["metrics"] for p in desk  # this round's picks
                                                                                             if p.get("round") == o.get("round")
                                                                                             and p.get("kind") != "major" and not p.get("redteam")],
                                                                             self._filters())) else {}),
                           **{k: _r(o["metrics"].get(k)) for k in DESK_NUM_KEYS if isinstance(o["metrics"].get(k), (int, float))
                              and not isinstance(o["metrics"].get(k), bool)}}
                          for o in desk]}
        if self.topup:
            s["pending_research_topup"] = {"needed_usd": self.topup["needed_usdc"], "suggested_usd": self.topup["suggest_usdc"]}
        why = self.amend_block_reason()
        if self.mandate is not None:
            s["can_change_settings"] = why is None
            if why:
                s["change_settings_blocked_because"] = why
            s["mandate_version"] = self.versions
        if self.engine is not None:
            st = self.engine.status()
            s["pending_cards"] = [{"code": c["code"], "coin_number": c.get("coin_number"), "buy_usd": c["amount_usdc"],
                                   "seconds_left": c["seconds_left"], "guard": c["guard"]["decision"]}
                                  for c in st["cards"] if c["status"] == "pending"]
            s["bought_usd"] = round(st["spent_usdc"], 2)
            s["positions"] = [{"number": i + 1, "bought_usd": round(p["entry_usdc"] / 1e6, 2),
                               "sold_back_usd": round(p["realized_usdc"] / 1e6, 2),
                               "remaining_position_pct": round(100 * p["held_tokens"] / p["initial_tokens"]) if p["initial_tokens"] else 0,
                               "price_vs_entry_multiple": round(p["last_ratio"], 3) if p.get("last_ratio") else None,
                               # P&L after fees and slippage: the same number as the top bar's Today and the spoken offers
                               "pnl_usd": round(p["pnl_usdc"] / 1e6, 2) if p.get("pnl_usdc") is not None else None,
                               "pnl_pct": round(100 * p["pnl_usdc"] / p["entry_usdc"], 1) if p.get("pnl_usdc") is not None and p["entry_usdc"] else None,
                               "status": p["status"]}
                              for i, p in enumerate(st["positions"])]
        return s

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        for t in list(self._bg) + list(self._build_tasks):
            if not t.done():
                t.cancel()
        for t in self._price_tasks:
            if t and not t.done():
                t.cancel()
        if self._topup_fut and not self._topup_fut.done():
            self._topup_fut.set_result(0.0)
        if self.engine is not None and hasattr(self.engine, "close"):
            await self.engine.close()


# ---------------- helpers (numbers and fixed English only) ----------------
def _r(v):
    return round(v, 2) if isinstance(v, float) else v


def _pool_of(c: dict) -> str | None:
    from backend.discovery.dexscreener import valid_mint
    return valid_mint((c.get("metrics") or {}).get("pair"))


def _safe_dex(x) -> str:
    return x if isinstance(x, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", x) else ""


def _clean(d: dict) -> dict:
    """Numbers / booleans only, plus derived age and volume-to-liquidity."""
    out = {k: v for k, v in d.items() if v is None or isinstance(v, bool) or isinstance(v, (int, float))}
    ms = out.get("pair_created_at_ms")
    if isinstance(ms, (int, float)) and ms > 0:
        out["age_hours"] = round(max(0.0, (time.time() * 1000 - ms) / 3_600_000), 2)
    liq, vol = out.get("liquidity_usd"), out.get("volume_h24")
    if isinstance(liq, (int, float)) and liq > 0 and isinstance(vol, (int, float)):
        out["volume_to_liquidity"] = round(vol / liq, 2)
    return out


def _card_metrics(m: dict) -> dict:
    return {k: m.get(k) for k in NUM_KEYS if isinstance(m.get(k), (int, float)) and not isinstance(m.get(k), bool)}


def desk_signals(m: dict, paid_promo: bool, f) -> dict:
    """Short plain-English pass / caution notes from numbers (code-written; never third-party text)."""
    good, warn = [], []
    g = m.get
    if g("lp_burned_pct") is not None and (g("lp_burned_pct") or 0) + (g("lp_locked_pct") or 0) >= 95:
        good.append(f"LP {g('lp_burned_pct'):.0f}% burned" + (f", {g('lp_locked_pct'):.0f}% locked" if g("lp_locked_pct") else ""))
    if g("mint_revoked") and g("freeze_revoked"):
        good.append("mint and freeze authority revoked")
    if isinstance(g("roundtrip_loss_pct"), (int, float)):
        (good if g("roundtrip_loss_pct") <= 3 else warn).append(f"buy-then-sell test loses {g('roundtrip_loss_pct'):.1f}%")
    if isinstance(g("holders"), (int, float)):
        good.append(f"{g('holders'):,.0f} holders")
    # limit-based notes: a caution only near or over the mandate's own limit (desk.NEAR_LIMIT), always with the limit
    if isinstance(g("top10_pct"), (int, float)) and f is not None:
        t = g("top10_pct")
        (warn if near_limit(t, f.max_top10_pct) else good).append(f"top 10 hold {t:.1f}%, {vs_limit(t, f.max_top10_pct)}")
    if isinstance(g("dev_pct"), (int, float)) and f is not None and near_limit(g("dev_pct"), f.max_dev_pct):
        warn.append(f"dev holds {g('dev_pct'):.1f}%, {vs_limit(g('dev_pct'), f.max_dev_pct)}")
    vl = g("volume_to_liquidity")
    if isinstance(vl, (int, float)) and f is not None:
        if near_limit(vl, f.max_volume_to_liquidity):
            warn.append(f"24h volume is {vl:.1f}x liquidity, {vs_limit(vl, f.max_volume_to_liquidity, 'x')}: possible wash trading")
        elif vl >= 0.5:
            good.append(f"active trading ({vl:.1f}x liquidity in 24h)")
    if paid_promo:
        warn.append("paid promotion on DexScreener (boosted listing)")
    if isinstance(g("age_hours"), (int, float)) and g("age_hours") < 24:
        warn.append(f"very new: {g('age_hours'):.0f} hours old")
    for k, label, lim in (("price_change_h1", "1h", -15), ("price_change_h24", "24h", -30)):
        if isinstance(g(k), (int, float)) and g(k) <= lim:
            warn.append(f"down {abs(g(k)):.0f}% in {label}")
    if isinstance(g("price_change_h24"), (int, float)) and g("price_change_h24") >= 100:
        warn.append(f"up {g('price_change_h24'):.0f}% in 24h: chasing a pump is risky")
    b, sl = g("buys_h1"), g("sells_h1")
    if isinstance(b, int) and isinstance(sl, int) and b + sl >= 20:
        if sl > 1.5 * b:
            warn.append(f"sellers outnumber buyers in the last hour ({sl} sells vs {b} buys)")
        elif b > 1.5 * sl:
            good.append(f"more buys than sells in the last hour ({b} vs {sl})")
    if g("known_rugger") is False:
        good.append("dev not on the known-rugger list")
    return {"good": good[:6], "caution": warn[:6]}


def refilter_replay(coins: list[dict], m) -> list[dict]:
    """Rehearsal replay reuses a recorded discovery run; re-apply the market filters of the *current* mandate so an amended
    hunt (e.g. a lower market-cap max) changes the result. Coins that only failed old market filters are not re-tested
    (no mint / LP / sell simulation in replay) and stay failed with a note."""
    from types import SimpleNamespace
    from backend.discovery.free_checks import check_market
    out = []
    for c in coins:
        c = json.loads(json.dumps(c))
        met = c.get("metrics") or {}
        pair = SimpleNamespace(liquidity_usd=met.get("liquidity_usd"), market_cap=met.get("market_cap"),
                               volume_h24=met.get("volume_h24"), pair_created_at_ms=met.get("pair_created_at_ms"))
        rs = check_market(SimpleNamespace(pair=pair if met else None), m)
        if c.get("status") == "pending_research" and rs:
            c["status"], c["failed_stage"] = "fail_free", "market"
            c["reasons"] = [f"[market] {r}" for r in rs]
        elif c.get("status") == "fail_free" and c.get("failed_stage") == "market" and not rs:
            c["reasons"] = (c.get("reasons") or [])[:3] + ["[replay] passes the new market filters but is not re-tested in rehearsal"]
        out.append(c)
    return out


async def _vol_stop(pool):
    from backend.market.volatility import suggest_for_pool
    return await suggest_for_pool(pool)


def _with_injection_flag(sig: dict, o: dict) -> dict:
    if o.get("injection"):
        from backend.copilot.redteam import CAUTION
        sig = {"good": sig["good"], "caution": [CAUTION] + sig["caution"][:5]}
    return sig
