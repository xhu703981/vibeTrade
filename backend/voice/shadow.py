"""Shadow listener (旁听通道): read the user's live words while they are still talking and do safe prework.

Input: tentative (partial) transcripts and finalized transcripts streamed by the browser (POST /voice/partial).
Eleven Realtime v1 Max does not emit tentative transcripts (checked with scripts/partials_probe.py); it emits a
finalized user_transcript per phrase, ~0.4 s after the phrase ends and ~1.1 s before the agent's tool call. So in
practice the shadow listener works on those per-phrase finals plus VAD, and on partials when a mode provides them.

Hard rule: NOTHING commits here. No mandate / filter change, no research round, no paid research (x402), no card, no
swap, no approval. Commits still happen only through the finalized-turn tool calls, verified against the raw words.
Allowed prework (free and reversible):
  - stage a deterministic reading of the words for the UI ("Hearing: market cap < $300k");
  - prime the mandate extractor with exactly the prompt the tool call will use (final words only, a pure cache;
    a different utterance or stage misses) -- the one cost is an LLM call that is usually needed a second later;
  - warm the free GeckoTerminal candle cache for desk coins the user names.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.execute.cards import normalize_utterance
from backend.voice.verify import _desk_norm, verify_sell_all

MARKS = ("[System update]", "[URGENT update]")
HEARING_TTL_S = 8.0

_NUM = r"\$?\s*(\d+(?:[.,]\d+)*)\s*(k|m|mm|thousand|million|hundred thousand|hundred)?\b"
_LT = r"(?:below|under|less than|lower than|at most|no more than|max(?:imum)?(?: of)?|up to|<)"
_GT = r"(?:above|over|more than|greater than|at least|min(?:imum)?(?: of)?|>)"
_MCAP = r"(?:market\s*cap|mcap|market\s*capitali[sz]ation)"

FILTER_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("filters.market_cap_usd.max", re.compile(_MCAP + r"\s*(?:to\s+|of\s+|is\s+|be\s+)?" + _LT + r"\s*" + _NUM, re.I), "market cap < {v}"),
    ("filters.market_cap_usd.max", re.compile(_LT + r"\s*" + _NUM + r"\s*(?:dollars?\s*)?(?:in\s+)?" + _MCAP, re.I), "market cap < {v}"),
    ("filters.market_cap_usd.min", re.compile(_MCAP + r"\s*(?:to\s+|of\s+|is\s+|be\s+)?" + _GT + r"\s*" + _NUM, re.I), "market cap > {v}"),
    ("filters.market_cap_usd.min", re.compile(_GT + r"\s*" + _NUM + r"\s*(?:dollars?\s*)?(?:in\s+)?" + _MCAP, re.I), "market cap > {v}"),
    ("filters.min_liquidity_usd", re.compile(r"(?:min(?:imum)?\s+)?liquidity\s*(?:to\s+|of\s+|is\s+|be\s+)?(?:" + _GT + r"|at)?\s*" + _NUM, re.I), "liquidity ≥ {v}"),
    ("filters.min_liquidity_usd", re.compile(_GT + r"\s*" + _NUM + r"\s*(?:dollars?\s*)?(?:of\s+|in\s+)?liquidity", re.I), "liquidity ≥ {v}"),
    ("filters.min_holders", re.compile(_GT + r"\s*" + _NUM + r"\s*holders", re.I), "holders ≥ {n}"),
    ("filters.min_holders", re.compile(r"holders\s*(?:to\s+)?" + _GT + r"\s*" + _NUM, re.I), "holders ≥ {n}"),
    ("filters.max_top10_pct", re.compile(r"top\s*(?:10|ten)(?:\s+holders?)?\s*(?:share\s*)?(?:to\s+)?" + _LT + r"\s*(\d+(?:\.\d+)?)\s*(%|percent)", re.I), "top-10 ≤ {p}%"),
    ("filters.token_age_hours_min", re.compile(r"(?:older than|at least|age\s*(?:above|over|of at least|>=?)?)\s*(\d+(?:\.\d+)?)\s*(hours?|h|days?)\b", re.I), "age ≥ {h}h"),
]
STOP_RESEARCH = re.compile(r"\b(stop|cancel|abort|halt|kill)\b[\w\s']{0,14}?\b(research|search|round|looking|hunt)\b"
                           r"|\b(stop|never mind|forget it)\b\s*(looking|searching)\b|停止研究|别找了|停止搜索", re.I)
NEW_ROUND = re.compile(r"\b(another|new|next)\s+(research\s+)?round\b|\bresearch\s+again\b|\bstart\s+(the\s+)?research\b"
                       r"|\bfind\s+(me\s+)?(new|other|more)\s+coins\b|\brun\s+it\b", re.I)
GUARD_WORDS = re.compile(r"\b(budget|total|per[\s-]*trade|max(?:imum)? per|fee|stop[\s-]*loss|take[\s-]*profit|sell half|2x|3x)\b", re.I)
FILLER = re.compile(r"^(?:\s*(?:h+m+|u+m+|u+h+|e+r+m*|okay|ok|so|well|let me think|let me see|hold on|wait|right|yeah)[\s,.!?…-]*)+$", re.I)
APPROVE = re.compile(r"\b(approve[ds]?|yes|go ahead|do it|confirm)\b", re.I)
NEGATED = re.compile(r"\b(don'?t|do not|not|never|no)\b", re.I)

# Side talk: words clearly meant for someone else in the room / on the phone (a cheap hint, not a gate)
SIDE_TALK = re.compile(
    r"^\s*(hey|hi|yo|ok(ay)?)\s+(mom|mum|dad|honey|babe|guys|man)\b|\b(not you|i'?m (on a call|talking to (someone|my)))\b"
    r"|\b(hold on|one sec(ond)?|just a (sec|second|minute)),?\s+(mom|dad|honey|babe|guys)\b"
    r"|\b(dinner|coffee|lunch)\b.{0,20}\b(ready|later|tonight)\b|^\s*(mom|dad|honey|babe)[,!]", re.I)
TRADING_WORDS = re.compile(r"\b(coin|token|buy|sell|approve|research|filter|market\s*cap|liquidity|holders|stop|profit|budget|"
                           r"dollars?|cents?|card|trade|position|status|exit|confirm|round)\b|\d", re.I)


def _usd(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:g}M"
    if v >= 1e3:
        return f"${v / 1e3:g}k"
    return f"${v:g}"


def _value(num: str, unit: str | None) -> float | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    u = (unit or "").lower()
    mult = {"k": 1e3, "thousand": 1e3, "hundred thousand": 1e5, "hundred": 1e2, "m": 1e6, "mm": 1e6, "million": 1e6}.get(u, 1)
    return v * mult


def _norm(text: str) -> str:
    u = normalize_utterance(text)
    # "3 hundred thousand" -> "300 thousand"; "1 hundred" -> "100"
    u = re.sub(r"\b(\d+(?:\.\d+)?)\s+hundred\s+thousand\b", lambda m: f"{float(m[1]) * 100:g} thousand", u, flags=re.I)
    u = re.sub(r"\b(\d+(?:\.\d+)?)\s+hundred\b", lambda m: f"{float(m[1]) * 100:g}", u, flags=re.I)
    return u


def filter_diff(text: str) -> list[tuple[str, float, str]]:
    """Deterministic reading of filter changes in the words: [(path, value, label)]. Staging only."""
    u = _norm(text)
    out: dict[str, tuple[str, float, str]] = {}
    for path, rx, label in FILTER_PATTERNS:
        if path in out:
            continue
        m = rx.search(u)
        if not m:
            continue
        if path in ("filters.max_top10_pct",):
            v = float(m[1])
            out[path] = (path, v, label.format(p=f"{v:g}"))
        elif path == "filters.token_age_hours_min":
            v = float(m[1]) * (24 if m[2].lower().startswith("d") else 1)
            out[path] = (path, v, label.format(h=f"{v:g}"))
        else:
            v = _value(m[1], m[2])
            if v is None:
                continue
            if path == "filters.min_holders":
                out[path] = (path, v, label.format(n=f"{v:g}"))
            else:
                if v < 1000 and not m[2]:
                    continue   # "under 30" is not a market cap / liquidity in dollars; let the extractor decide
                out[path] = (path, v, label.format(v=_usd(v)))
    return list(out.values())


@dataclass
class Intent:
    kind: str                     # filter_change | new_round | guard_change | stop_research | sell_all | approve | coin_ref | mandate_answer
    label: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, **self.detail}


def parse_intent(text: str, ctx: dict) -> Intent | None:
    """Deterministic, regex-first reading of (possibly partial) words. ctx: frozen, desk_open (list of coin numbers),
    pending_codes, round_running. Never decides anything; the result is only shown and used to pick prework."""
    t = (text or "").strip()
    if not t or t.startswith(MARKS):
        return None
    if verify_sell_all(t) is None:
        return Intent("sell_all", "sell all positions")
    if STOP_RESEARCH.search(t) and ctx.get("round_running"):
        return Intent("stop_research", "stop the research")
    nu = normalize_utterance(t)
    codes = [c for c in ctx.get("pending_codes", []) if re.search(rf"(?<!\d){c}(?!\d)", nu)]
    if codes and APPROVE.search(nu) and not NEGATED.search(nu):
        return Intent("approve", f"approve card {codes[0]}", {"code": codes[0]})
    diff = filter_diff(t)
    if diff:
        return Intent("filter_change", ", ".join(x[2] for x in diff), {"changes": {p: v for p, v, _ in diff}})
    if ctx.get("frozen") and GUARD_WORDS.search(t):
        return Intent("guard_change", "a settings change")
    if ctx.get("frozen") and NEW_ROUND.search(t):
        return Intent("new_round", "a new research round")
    open_nums = ctx.get("desk_open") or []
    if open_nums:
        nums = sorted({int(n) for n in re.findall(r"\bcoins?\s*(\d{1,2})\b", _desk_norm(t)) if int(n) in open_nums})
        if nums:
            return Intent("coin_ref", "coin " + ", ".join(map(str, nums)), {"coins": nums})
    if not ctx.get("frozen") and not FILLER.match(t):
        return Intent("mandate_answer", "")       # setup: every answer goes to the extractor; nothing to show
    return None



# Turns where the agent will call submit_mandate_fields / confirm_mandate and may guess numbers first.
_GATE_KINDS = ("mandate_answer", "filter_change", "guard_change", "new_round")


def gate_speech(final: bool, text: str, it: Intent | None, ctx: dict) -> bool:
    """The browser should mute guessed pre-tool speech until the official tool speak arrives."""
    if not final:
        return False
    t = (text or "").strip()
    if not t or t.startswith(MARKS) or FILLER.match(t):
        return False
    if it and it.kind in _GATE_KINDS:
        return True
    # "confirm" during first setup (label-less mandate_answer is already covered) or an amendment
    from backend.mandate.dialogue import is_confirmation
    if is_confirmation(t) and (not ctx.get("frozen") or ctx.get("amending")):
        return True
    return False


def side_talk(text: str) -> bool:
    """Cheap hint that the words were for someone else (no trading words, addressed to a person)."""
    t = (text or "").strip()
    return bool(t) and bool(SIDE_TALK.search(t)) and not TRADING_WORDS.search(re.sub(r"^\s*\w+[,!]?\s*", "", t))


class ShadowListener:
    """Stages the latest reading of the user's words. ctx() -> dict (see parse_intent); session() -> MandateSession;
    warm(pool) -> awaitable (free candle prefetch); pools() -> {coin number: pool}."""

    def __init__(self, ctx: Callable[[], dict], session: Callable[[], Any], warm: Callable[[str], Any] | None = None,
                 pools: Callable[[], dict[int, str]] | None = None, speculate_extract: bool = True,
                 now: Callable[[], float] = time.time):
        self.ctx, self.session, self.warm, self.pools = ctx, session, warm, pools
        self.speculate_extract = speculate_extract
        self.now = now
        self.seq = -1
        self.staged: dict | None = None
        self._tasks: set[asyncio.Task] = set()
        self._warmed: dict[str, float] = {}
        self.stats = {"heard": 0, "finals": 0, "primed": 0, "matched": 0, "mismatched": 0}

    def hearing(self) -> dict | None:
        s = self.staged
        if s and self.now() - s["at"] <= HEARING_TTL_S:
            return {k: s[k] for k in ("kind", "label", "final", "at")}
        return None

    def hear(self, text: str, final: bool, seq: int) -> dict:
        if seq <= self.seq and not final:
            return {"ok": True, "stale": True, "hearing": self.hearing()}
        self.seq = max(self.seq, seq)
        self.stats["heard"] += 1
        if final:
            self.stats["finals"] += 1
        ctx = self.ctx()
        it = parse_intent(text, ctx)
        if it is None or not it.label:
            self.staged = None if final or it is None else self.staged
        else:
            self.staged = {**it.as_dict(), "text": text, "final": final, "at": self.now()}
        self._prework(it, text, final, ctx)
        return {"ok": True, "hearing": self.hearing(), "side_talk": bool(final and side_talk(text)),
                "gate_speech": gate_speech(final, text, it, ctx)}

    def _prework(self, it: Intent | None, text: str, final: bool, ctx: dict) -> None:
        if it is None:
            return
        if it.kind == "coin_ref" and self.warm and self.pools:
            pools = self.pools()
            for n in it.detail.get("coins", []):
                pool = pools.get(n)
                if pool and self.now() - self._warmed.get(pool, 0) > 60:
                    self._warmed[pool] = self.now()
                    self._spawn(self.warm(pool))
        # extraction cache: only for finalized words that will very likely become submit_mandate_fields
        if final and self.speculate_extract and it.kind in ("filter_change", "new_round", "guard_change", "mandate_answer"):
            s = self.session()
            from backend.mandate.dialogue import is_confirmation
            if it.kind == "mandate_answer" and is_confirmation(text):
                return            # "confirm" goes to confirm_mandate, not to the extractor
            if ctx.get("amend_blocked"):
                return
            try:
                if s.prime_extract(text):
                    self.stats["primed"] += 1
            except Exception:  # noqa: BLE001  prework must never break the conversation
                pass

    def _spawn(self, aw) -> None:
        async def run():
            try:
                await aw
            except Exception:  # noqa: BLE001
                pass
        t = asyncio.ensure_future(run())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def committed(self, utterance: str | None) -> None:
        """A finalized tool call arrived: the staged reading is matched (its prework was used) or discarded."""
        s = self.staged
        if s is not None:
            same = (utterance or "").strip() == s["text"].strip()
            self.stats["matched" if same else "mismatched"] += 1
        self.staged = None

    def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
