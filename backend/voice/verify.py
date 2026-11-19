"""Deterministic checks on the user's raw spoken words (M6): system-broadcast echoes, research top-ups, emergency exit.

The frontend sends "the user's speech-recognition text for this turn" (or what they typed) separately; it is never
an agent tool argument. System broadcasts (backend events the agent reads aloud via sendUserMessage) always start
with SYSTEM_MARK / URGENT_MARK; their text may contain 'To approve, say "approve 47"' and must never count as the
user's own words.
English is the primary path; Chinese phrases remain as extra aliases.
"""
from __future__ import annotations

import re

from backend.execute.cards import REJECT_WORDS, normalize_utterance

SYSTEM_MARK = "[System update]"
URGENT_MARK = "[URGENT update]"
LEGACY_MARKS = ("【系统播报】", "【紧急播报】")
_ECHO_SNIPPETS = (SYSTEM_MARK, URGENT_MARK, *LEGACY_MARKS,
                  "to approve, say", "counts as a reject", "how much should i add", "to add it, say", "details on screen",
                  "my proposed coin filters",
                  "批准请说", "秒内不回应就算拒绝", "要追加多少")

TOPUP_WORDS = re.compile(r"\b(approve[ds]?|yes|yeah|yep|ok|okay|sure|add|top (it )?up|go ahead|fine)\b"
                         r"|批准|同意|确认|追加|加|好的?|行|可以", re.I)
TOPUP_NEGATED = re.compile(
    r"\b(don'?t|do not|no|not|never|nope|can'?t|cannot|won'?t)\b(\s+[\w']+){0,2}?\s*(approv|add|top|go ahead|need)"
    r"|^\s*no\b|\bno more\b|\bdon'?t\b"
    r"|(不|别|没|勿|不要)\s*(用|要|需要)?\s*(批准|同意|追加|加)", re.I)
_AMOUNT = re.compile(r"\$?\s*(\d+(?:\.\d+)?)\s*(cents?|毛|角|块|元|美元|美金|刀|dollars?|bucks?|usd|usdc)?", re.I)
SELL_ALL = re.compile(
    r"\bsell\s+(it\s+)?all\b|\bsell\s+everything\b|\bsell\s+all\s+(of\s+)?(it|them|my|the)\b|\bsell\s+every\s+(position|coin|token)"
    r"|\bexit\s+(all|everything)\b|\bemergency\s+exit\b|\bclose\s+(all|everything)\b|\bliquidate\b|\bdump\s+(it\s+)?all\b"
    r"|\bget\s+me\s+out\b|\bpanic\s+sell\b"
    r"|全部卖|全卖|都卖|卖光|卖掉全部|全部清仓|清仓|一键清仓|紧急退出", re.I)
SELL_NEGATED = re.compile(
    r"\b(don'?t|do not|never|not|no|can'?t|cannot|won'?t|shouldn'?t|hold off on)\b(\s+[\w']+){0,2}?\s+(sell|exit|close|liquidate|dump)"
    r"|\bnot\s+yet\b"
    r"|(不|别|没|勿|不要|先别)\s*(要)?\s*(全部|都)?\s*(卖|清仓)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def echo_problem(utterance: str | None, recent_speak: list[str] = ()) -> str | None:
    """If the words look like an echo of a system broadcast, return why."""
    u = utterance or ""
    lu = u.lower()
    if any(x.lower() in lu for x in _ECHO_SNIPPETS):
        return "that sentence came from a system broadcast, not from you"
    nu = _norm(u)
    # a short command ("approve 47") is what the broadcast asks the user to say, not an echo; only longer repeats count
    if len(nu) >= 8 and len(re.findall(r"\w+", u)) >= 4:
        for s in recent_speak:
            ns = _norm(s)
            if ns and (nu in ns):
                return "that sentence repeats a system broadcast, so it does not count as your answer"
    return None


def amounts_in(utterance: str) -> list[float]:
    """Amounts in the words: '50 cents' = 0.5, '$0.5' = 0.5, 'half a dollar' = 0.5, '5 毛' = 0.5, '1 块' = 1."""
    u = normalize_utterance(utterance).replace("半", "0.5") if utterance else ""
    out = []
    for m in _AMOUNT.finditer(u):
        v = float(m[1])
        unit = (m[2] or "").lower()
        if unit.startswith("cent"):
            v /= 100
        elif unit in ("毛", "角"):
            v /= 10
        out.append(round(v, 6))
    return out


def verify_topup(utterance: str | None, amount_usdc: float) -> str | None:
    """Approve a research top-up: needs an agree / add word and the amount; a negation or reject word fails. None = ok."""
    u = normalize_utterance(utterance or "")
    if not u:
        return "I did not get your exact words"
    if TOPUP_NEGATED.search(u) or REJECT_WORDS.search(u):
        return "your words meant not to add more"
    if not TOPUP_WORDS.search(u):
        return "I did not hear you agree to the top-up"
    if not any(abs(a - amount_usdc) < 1e-9 for a in amounts_in(u)):
        return f"I did not hear the top-up amount of {amount_usdc:g} dollars"
    return None


def verify_sell_all(utterance: str | None) -> str | None:
    u = normalize_utterance(utterance or "")
    if not u:
        return "I did not get your exact words"
    if SELL_NEGATED.search(u):
        return "your words meant not to sell"
    if not SELL_ALL.search(u):
        return "I did not hear \"sell all\" or \"emergency exit\""
    return None


# ---------------- M6+: buy size for a coin that passed research ----------------
BUY_MAX_RE = re.compile(
    r"\b(the\s+)?(max(imum)?|cap|limit|full\s+(amount|cap|limit)|whole\s+(limit|cap)|all\s+of\s+(the|my)\s+(per[\s-]trade\s+)?(limit|cap|max))\b"
    r"|最多|上限|满额", re.I)
BUY_NEGATED = re.compile(r"\b(don'?t|do not|not|never|no)\b(\s+[\w']+){0,2}?\s+(buy|purchase|get)\b|\bskip\b|\bpass\b|不买|不要买|跳过", re.I)


def verify_buy_size(utterance: str | None, amount_usdc: float | None, cap_usdc: float) -> tuple[float | None, str | None]:
    """Read the buy size from the user's own words: an amount ("buy 5 dollars", "one cent") or "the max".
    Returns (amount, None) or (None, reason). The agent's amount is only used to pick between several numbers."""
    u = normalize_utterance(utterance or "")
    if not u:
        return None, "I did not get your exact words"
    if BUY_NEGATED.search(u):
        return None, "your words meant not to buy"
    amts = [a for a in amounts_in(u) if a > 0]
    if BUY_MAX_RE.search(u) and not amts:
        return cap_usdc, None
    if amount_usdc is not None and any(abs(a - amount_usdc) < 1e-9 for a in amts):
        return float(amount_usdc), None
    if len(amts) == 1:
        return amts[0], None
    if not amts:
        return None, "I did not hear an amount"
    return None, "I heard more than one amount"


SIM_RE = re.compile(r"\b(simulate|test|trigger|fake)\b[\w\s,-]{0,20}?\b(take[\s-]*profit|tp|stop[\s-]*loss|sl)\b", re.I)


def verify_price_test(utterance: str | None, kind: str) -> str | None:
    m = SIM_RE.search(utterance or "")
    if not m:
        return "I did not hear \"simulate take profit\" or \"simulate stop loss\""
    said = "take_profit" if re.match(r"take|tp", m[2], re.I) else "stop_loss"
    return None if said == kind else "the words asked for the other test"


# ---------------- selection desk: "coin 1 and coin 3, 5 dollars each" ----------------
_ORDINALS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6", "seventh": "7", "eighth": "8",
             "ninth": "9", "tenth": "10"}
_UNIT = r"(?:dollars?|bucks?|usdc|usd|cents?)"
_MONEY_RE = re.compile(rf"\$\s*(\d+(?:\.\d+)?)|(?<![\d.])(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.I)
_MAX_RE = re.compile(r"\b(the\s+)?(max(imum)?|cap|limit|full amount)\b|最多|上限", re.I)
_LIST_TAIL = rf"(?:\s*(?:,|and|&|plus|or)\s*(?:the\s+)?(?:coins?\s*|tokens?\s*|numbers?\s*|#\s*)?\d+(?![\d.])(?!\s*{_UNIT})(?!\s*%))*"
_COIN_GROUP_RE = re.compile(rf"(?:\b(?:coins?|tokens?|numbers?)\s*|#\s*)(\d+)(?![\d.])(?!\s*{_UNIT}){_LIST_TAIL}", re.I)
_ALL_RE = re.compile(r"\b(all|every|each)\s+(of\s+)?(them|coins?|the coins|qualified( coins)?|(one|ones)|three|four|five|two)\b|\bboth\b|全部|都买", re.I)
_EACH_RE = re.compile(r"\b(each|apiece|a piece|per coin|for both|for all|every one|everyone|each one)\b|各|每个", re.I)
_SKIP_RE = re.compile(r"\b(skip|pass( on)?|drop|not interested( in)?|none of them|no thanks|don'?t want|do not want|leave( out)?)\b|跳过|不要|不买", re.I)
_SKIP_SPAN_RE = re.compile(rf"\b(?:skip|pass on|drop|not|except|but not|without|leave out)\s+(?:the\s+)?((?:coins?\s*|tokens?\s*|numbers?\s*|#\s*)?\d+(?![\d.]){_LIST_TAIL})", re.I)
DESK_NEGATED = re.compile(r"\b(don'?t|do not|never|no)\s+(buy|order|purchase|get)\b|\bnot (buy|order)\b|不买", re.I)


_COIN_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
               "eleven": 11, "twelve": 12}
_CW = "|".join(_COIN_WORDS)
_COIN_WORD_GROUP = re.compile(r"\b(coins?|numbers?|tokens?)\s+((?:" + _CW + r"|\d+)(?:\s*(?:,|and|&|or)\s*(?:" + _CW + r"|\d+))*)\b")


def _coin_words_to_digits(u: str) -> str:
    """'coin one five dollars' must stay coin 1 / 5 dollars: convert coin-number words before the generic number merge
    (which would read 'one five' as 15)."""
    def grp(m: re.Match) -> str:
        body = re.sub(r"\b(" + _CW + r")\b", lambda w: str(_COIN_WORDS[w[1]]), m[2])
        return f"{m[1]} {body}"
    return _COIN_WORD_GROUP.sub(grp, u)


_WORD_DIGIT = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
               "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_SPOKEN_DIGIT = r"(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine|\d)"
_SPOKEN_POINT_RE = re.compile(
    r"\b(?:(zero|oh|o|0)\s+)?point\s+(" + _SPOKEN_DIGIT + r"(?:[\s-]+" + _SPOKEN_DIGIT + r")*)\b", re.I)
_DIGIT_POINT_RE = re.compile(r"\b(\d+)\s+point\s+((?:\d\s*)*\d)\b", re.I)
_PENNY_RE = re.compile(r"\b(?:an?\s+)?penn(?:y|ies)\b", re.I)


def _fold_spoken_money(u: str) -> str:
    """Turn spoken decimals and pennies into digit amounts before generic number-word folding.

    'zero point zero one dollars' otherwise becomes '0 point 01 dollars' and is read as $1.
    This is desk-only; mandate cent / vague-marker extraction is not changed.
    """
    u = _PENNY_RE.sub("0.01 dollars", u)

    def spoken(m: re.Match) -> str:
        frac = "".join(_WORD_DIGIT.get(p.lower(), p) for p in re.split(r"[\s-]+", m[2].strip()) if p)
        return f"0.{frac}" if frac else m[0]

    u = _SPOKEN_POINT_RE.sub(spoken, u)

    def digits(m: re.Match) -> str:
        frac = re.sub(r"\s+", "", m[2])
        return f"{m[1]}.{frac}"

    return _DIGIT_POINT_RE.sub(digits, u)


def _desk_norm(utterance: str | None) -> str:
    u = _fold_spoken_money((utterance or "").lower())
    u = normalize_utterance(_coin_words_to_digits(u)).lower()
    u = _fold_spoken_money(u)  # leftover '0 point 01' after number-word folding
    u = re.sub(r"\b(" + "|".join(_ORDINALS) + r")\s+(one|1|coin|token)\b", lambda m: f"coin {_ORDINALS[m[1]]}", u)
    u = re.sub(r"\bnumber\s+", "coin ", u)
    # 'coin 1.' is a sentence period, not a decimal; otherwise the coin regex rejects it
    u = re.sub(r"(?<=\d)\.(?!\d)", "", u)
    return u


def _nums_in(group: str) -> list[int]:
    return [int(x) for x in re.findall(r"(?<![\d.])(\d+)(?![\d.])", group)]


def _money_in(text: str) -> list[float]:
    out = []
    for m in _MONEY_RE.finditer(text):
        if m[1] is not None:
            out.append(round(float(m[1]), 6))
        else:
            v = float(m[2])
            out.append(round(v / 100 if m[3].lower().startswith("cent") else v, 6))
    return out


def parse_desk_order(utterance: str | None, open_numbers: list[int], cap_usdc: float) -> tuple[dict[int, float] | None, str | None]:
    """Read which desk coins to buy and how much each, only from the user's own words.
    Returns ({coin number: dollars}, None) or (None, reason). Deterministic, no LLM; amounts need a currency word, "$" or "the max"."""
    u = _desk_norm(utterance)
    if not u:
        return None, "I did not get your exact words"
    if DESK_NEGATED.search(u):
        return None, "your words meant not to buy"
    skipped = {n for m in _SKIP_SPAN_RE.finditer(u) for n in _nums_in(m[1])}
    u2 = _SKIP_SPAN_RE.sub(" ", u)
    groups = [(m.start(), m.end(), [n for n in _nums_in(m[0]) if n not in skipped]) for m in _COIN_GROUP_RE.finditer(u2)]
    groups = [g for g in groups if g[2]]
    if not groups:
        if _ALL_RE.search(u2):
            groups = [(0, 0, [n for n in open_numbers if n not in skipped])]
        elif len(open_numbers) == 1:
            groups = [(0, 0, list(open_numbers))]
        else:
            return None, "say which coin numbers, for example \"coin 1 and coin 3\""
    # amounts: money words / "$" / "the max", assigned to the coin group they follow (or the first group if said before it)
    amounts: list[tuple[int, float]] = [(m.start(), v) for m in _MONEY_RE.finditer(u2) for v in _money_in(m[0])]
    amounts += [(m.start(), float(cap_usdc)) for m in _MAX_RE.finditer(u2)]
    amounts.sort()
    if not amounts:
        return None, "I did not hear an amount in dollars"
    if len(amounts) == 1 or len({round(a, 6) for _, a in amounts}) == 1 and len(groups) == 1:
        coins = [n for g in groups for n in g[2]]
        if len(coins) > 1 and not _EACH_RE.search(u2):
            return None, "I heard one amount for several coins; say for example \"5 dollars each\""
        return {n: amounts[0][1] for n in coins}, None
    out: dict[int, float] = {}
    starts = [g[0] for g in groups] + [10**9]
    for i, (s0, _e, nums) in enumerate(groups):
        lo = 0 if i == 0 else s0
        seg = [a for p, a in amounts if lo <= p < starts[i + 1]]
        if len(seg) != 1:
            return None, "I could not match one amount to each coin; say for example \"coin 1 5 dollars, coin 3 2 dollars\""
        if len(nums) > 1 and not _EACH_RE.search(u2):
            return None, "I heard one amount for several coins; say for example \"5 dollars each\""
        for n in nums:
            out[n] = seg[0]
    return out, None


def parse_desk_skip(utterance: str | None, open_numbers: list[int]) -> tuple[list[int] | None, str | None]:
    """Skipping is the safe direction: "skip coin 2", "skip the rest", "none of them"."""
    u = _desk_norm(utterance)
    if not _SKIP_RE.search(u):
        return None, "I did not hear \"skip\""
    nums = [n for m in _COIN_GROUP_RE.finditer(u) for n in _nums_in(m[0])] or \
           [n for m in _SKIP_SPAN_RE.finditer(u) for n in _nums_in(m[1])]
    if not nums:
        nums = list(open_numbers)
    return sorted({n for n in nums if n in open_numbers}) or None, None if nums else "no matching coin"
