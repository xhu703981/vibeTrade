"""Confirmation cards: random two-digit code + check against the user's raw words.

Approve / change-amount requires all of (otherwise the approval is refused, the card stays pending and is re-read):
  - the card exists, is pending and not expired;
  - voice channel: the user's raw utterance (sent separately by the frontend, not a tool argument) contains an
    approve word and this card's two-digit code, with no negation; a change also needs the new amount spoken;
  - UI button channel (when enabled by the API switch): the request carries this card's code (read off the screen);
  - the amount is within the mandate's per-trade / total limits.
Reject and timeout need no check (they move in the safe direction). 3 failed checks in a row -> the card is voided.
"""
from __future__ import annotations

import re
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field

# English is the primary path; Chinese words remain as extra aliases.
APPROVE_WORDS = r"(\bapprov(e|ed|ing)\b|\bconfirm(ed)?\b|\bgo ahead\b|批准|同意|确认)"
MODIFY_WORDS = r"(\bchange\b|\bmodify\b|\bmake it\b|\bset it to\b|\badjust\b|改成|改为|改到|换成)"
NEGATED_APPROVE = re.compile(
    r"\b(don'?t|do not|does not|doesn'?t|not|never|no|can'?t|cannot|won'?t|wouldn'?t|shouldn'?t|hold off|stop)\b"
    r"(\s+[\w']+){0,2}?\s+(approv|confirm|go ahead)|\bdisapprov|\bunapprov"
    r"|(不|别|没|勿|不要)\s*(批准|同意|确认)", re.I)
REJECT_WORDS = re.compile(r"\b(reject(ed)?|cancel(led|ed)?|decline[d]?|deny|denied|refuse[d]?|skip( it)?|pass on|nope|no thanks)\b"
                          r"|拒绝|取消|算了", re.I)
MAX_FAILED_ATTEMPTS = 3

_CN_DIGITS = {"零": "0", "〇": "0", "一": "1", "幺": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5",
              "六": "6", "七": "7", "八": "8", "九": "9"}


def normalize_utterance(u: str) -> str:
    """Speech recognition often spells numbers out: convert English number words ("forty-seven", "four seven",
    "fifty cents") and Chinese numerals ("四七", "四十七") to Arabic digits."""
    u = _en_numbers(u.strip().replace("\u2019", "'"))
    u = re.sub(r"([一二三四五六七八九])十([一二三四五六七八九])", lambda m: _CN_DIGITS[m[1]] + _CN_DIGITS[m[2]], u)
    u = re.sub(r"([一二三四五六七八九])十", lambda m: _CN_DIGITS[m[1]] + "0", u)
    u = re.sub(r"十([一二三四五六七八九])", lambda m: "1" + _CN_DIGITS[m[1]], u)
    return "".join(_CN_DIGITS.get(ch, ch) for ch in u).replace("十", "10")


_EN_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                         "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_EN_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_EN_NUM_RE = re.compile(
    r"\b(?:(" + "|".join(_EN_TENS) + r")(?:[\s-]+(" + "|".join(list(_EN_UNITS)[1:10]) + r"))?|(" + "|".join(_EN_UNITS) + r"))\b",
    re.I)


def _en_numbers(u: str) -> str:
    """English number words 0-99 -> digits; runs of single spoken digits are joined ("four seven" -> "47")."""
    def one(m: re.Match) -> str:
        if m[1]:
            return str(_EN_TENS[m[1].lower()] + (_EN_UNITS[m[2].lower()] if m[2] else 0))
        return str(_EN_UNITS[m[3].lower()])
    digits = "|".join(list(_EN_UNITS)[:10])
    u = re.sub(rf"\b({digits})[\s-]+({digits})\b(?![\s-]+(?:{digits})\b)",
               lambda m: str(_EN_UNITS[m[1].lower()]) + str(_EN_UNITS[m[2].lower()]), u, flags=re.I)
    u = _EN_NUM_RE.sub(one, u)
    u = re.sub(r"\bhalf (a|one) (dollar|buck)s?\b", "0.5 dollars", u, flags=re.I)
    return u


def _code_re(code: str) -> re.Pattern:
    # "47", also "4 7" / "4-7" as ASR sometimes splits spoken digits
    # a trailing sentence period is fine ("approve 47."), a decimal part is not ("47.5")
    body = rf"{code[0]}[\s-]?{code[1:]}" if len(code) == 2 else re.escape(code)
    return re.compile(rf"(?<!\d)(?<!\d\.){body}(?!\d)(?!\.\d)")


def _has_code(u: str, code: str) -> bool:
    return _code_re(code).search(u) is not None


def verify_approval(utterance: str, code: str) -> str | None:
    """Return a failure reason; None = the raw words really approve this card. Deterministic rules, no LLM."""
    u = normalize_utterance(utterance)
    if not re.search(APPROVE_WORDS, u, re.I):
        return "your words did not include \"approve\""
    if NEGATED_APPROVE.search(u):
        return "your words were a negated approval"
    if REJECT_WORDS.search(u):
        return "your words also contained reject / cancel"
    if not _has_code(u, code):
        return "the code you said does not match the card" if re.search(r"\d", u) else "you did not say the card's two-digit code"
    return None


YES_RE = re.compile(r"\b(yes|yeah|yep|yup|sure|ok(ay)?|do it|go ahead|please do|sounds good|sell (it|half|them)|approve)\b"
                    r"|好的|可以|卖吧", re.I)
NO_RE = re.compile(r"\b(no|nope|not|don'?t|do not|never|wait|hold( on| off)?|cancel|reject|skip|stop)\b|不要|别|算了", re.I)


def verify_derisk_yes(utterance: str, code: str) -> str | None:
    """De-risking cards (sell part, take profit, raise the stop) only lower risk, so the user's plain "yes" to the
    co-pilot's offer approves them. Still deterministic: a yes-word, no negation, and if a number is said it must be
    this card's code. Buy / add cards never come here (they always need the code)."""
    u = normalize_utterance(utterance)
    if not YES_RE.search(u):
        return "your words did not include a yes"
    if NO_RE.search(re.sub(r"\bsell\b", " ", u)) or NEGATED_APPROVE.search(u):
        return "your words also contained a no or a negation"
    if re.search(r"\d", u) and not _has_code(u, code):
        return "the number you said is not this card's code"
    return None


def verify_modify(utterance: str, code: str, amount: float) -> str | None:
    u = normalize_utterance(utterance)
    if not (re.search(APPROVE_WORDS, u, re.I) or re.search(MODIFY_WORDS, u, re.I)):
        return "your words did not include \"approve\" or \"change\""
    if NEGATED_APPROVE.search(u) or REJECT_WORDS.search(u):
        return "your words were a negation / rejection"
    if not _has_code(u, code):
        return "the code you said does not match the card"
    rest = _code_re(code).sub(" ", u, count=1)  # the code counts once; if the amount equals the code it must be said twice
    nums = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(?!\d)(?!\.\d)", rest)
    if not any(abs(float(n) - amount) < 1e-9 for n in nums):
        return f"you did not say the new amount {amount:g}"
    return None


@dataclass
class Card:
    id: str
    code: str
    mint: str
    symbol_display: str
    amount_usdc: float
    expected_out: int                   # card quote: expected tokens out (base units)
    metrics: dict                       # numbers only
    sim: dict                           # simulation summary (numbers)
    guard: dict                         # guard verdict (code-generated reasons)
    created_at: float
    expires_at: float
    status: str = "pending"             # pending | approved | rejected | expired | voided | executed | failed
    failed_attempts: int = 0
    replaced_by: str | None = None
    history: list[dict] = field(default_factory=list)
    coin_number: int | None = None      # number of the coin on the selection desk (voice refers to coins by number)
    mandate_id: str | None = None       # mandate version the card was built under
    kind: str = "buy"                   # buy | sell | adjust_exit
    ticker: str | None = None           # allowlisted major ticker only
    sell_fraction: float | None = None  # 0-1, for sell cards
    adjust: dict | None = None          # {stop_loss_pct?, take_profit_pct?, break_even?}
    stop_loss_pct: float | None = None  # buy cards: the stop the position starts with (user-said, mandate, or volatility)
    stop_basis: str | None = None       # "you" | "mandate" | "atr" | "fallback"
    atr_pct: float | None = None        # the volatility number behind an "atr" stop
    risk_usdc: float | None = None      # risk-based sizing: what the user said they are willing to lose
    loss_plan_usdc: float | None = None   # buy cards: loss if the stop fires at the stop (amount x stop)
    loss_worst_usdc: float | None = None  # ... plus gap past the stop and the full allowed stop-loss sell slippage
    cap_note: str | None = None           # risk-sized buy cut down: "capped at the per-trade limit" / "... what's left to spend"
    derisk: bool = False                # sell part / take profit / raise stop: lowers risk, so a plain "yes" can approve it
    suspicion: dict | None = None       # JEV's most suspicious item for this coin {label, score}
    warning: str | None = None          # the same figure near or over a mandate limit as on the picks list, or None
    offer: str | None = None            # the co-pilot's own spoken offer that created this card ("Want to sell half?")

    def public(self) -> dict:
        d = asdict(self)
        d["seconds_left"] = max(0, round(self.expires_at - time.time()))
        return d


class CodeBook:
    """Two-digit code: random, never reusing any earlier code (expired cards' codes included)."""

    def __init__(self):
        self.used: set[str] = set()

    def new(self) -> str:
        free = [f"{i:02d}" for i in range(10, 100) if f"{i:02d}" not in self.used]
        if not free:
            raise RuntimeError("ran out of two-digit codes")
        c = secrets.choice(free)
        self.used.add(c)
        return c


def new_card_id() -> str:
    return uuid.uuid4().hex[:12]
