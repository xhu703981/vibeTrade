"""Extract a position-management action from the user's words.

Deterministic parse first, gpt-4o-mini fallback (same pattern as desk orders).
The backend validates and then creates a confirmation card; nothing executes here.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from backend.execute.cards import normalize_utterance
from backend.judge.llm import LLMError, chat_json
from backend.voice.verify import _coin_words_to_digits, _desk_norm

_HALF = re.compile(r"\b(half|50\s*%|fifty\s+percent)\b", re.I)
_ALL = re.compile(r"\b(all|everything|entire|full|100\s*%|the whole)\b", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", re.I)
_COIN = re.compile(r"\bcoin\s+(\d{1,3})\b", re.I)
_BE = re.compile(r"\bbreak[\s-]*even\b", re.I)
_SELL = re.compile(r"\b(sell|close|exit|dump|get out)\b", re.I)
_STOP = re.compile(r"\bstop(?:\s*loss)?\b", re.I)
_TP = re.compile(r"\b(take[\s-]*profit|take profit|tp)\b", re.I)
_MOVE = re.compile(r"\b(move|set|change|update|adjust|make)\b", re.I)
_CLOSE_ALL = re.compile(r"\b(close|sell|exit)\s+(everything|all(?:\s+positions)?|every position)\b", re.I)
_TICKER = re.compile(r"\b(jup|jto|pyth|bonk|wif|ray|cbbtc|wbtc|weth)\b", re.I)

EXTRACT_SYSTEM = """Extract a portfolio action for a Solana trading assistant.
Reply JSON only, one of:
{"kind":"sell","coins":[3],"fraction":0.5,"ticker":null}
{"kind":"sell","coins":"all","fraction":1}
{"kind":"set_stop","coins":[2],"stop_loss_pct":3,"break_even":false}
{"kind":"set_stop","coins":"all","break_even":true}
{"kind":"set_tp","coins":[1],"take_profit_pct":10}
{"kind":null,"why":"short reason"}
coins is a list of desk numbers, or "all". fraction is 0-1 (0.5 = half, 1 = all, 0.25 = 25%).
break_even means move the stop to the entry. take_profit_pct is the gain in percent (10 means 1.10x).
Never invent a coin that was not mentioned; if none was named and only one position is open, you may use that coin.
"""


@dataclass
class PositionAction:
    kind: str                          # sell | set_stop | set_tp
    coins: list[int] | str             # list or "all"
    fraction: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    break_even: bool = False
    ticker: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _coins(text: str, open_numbers: list[int]) -> list[int] | str | None:
    if re.search(r"\b(all of them|every(one| position)|all positions|everything)\b", text, re.I):
        return "all"
    nums = [int(n) for n in _COIN.findall(text)]
    nums = [n for n in nums if n in open_numbers]
    if nums:
        return nums
    return None


def parse_position_action(utterance: str | None, open_numbers: list[int],
                          tickers: dict[str, int] | None = None) -> tuple[PositionAction | None, str | None]:
    u = _desk_norm(utterance)
    if not u:
        return None, "I did not get your exact words"
    tickers = {k.lower(): v for k, v in (tickers or {}).items()}
    ticker = None
    m = _TICKER.search(u)
    if m and m.group(1).lower() in tickers:
        ticker = m.group(1).upper()
        coins: list[int] | str | None = [tickers[m.group(1).lower()]]
    else:
        coins = _coins(u, open_numbers)

    if _CLOSE_ALL.search(u) or (re.search(r"\b(sell|close)\s+all\b", u) and not _COIN.search(u) and not ticker):
        return PositionAction("sell", "all", fraction=1.0), None

    if _SELL.search(u) and not _STOP.search(u) and not _TP.search(u):
        if _HALF.search(u):
            frac = 0.5
        elif _ALL.search(u):
            frac = 1.0
        else:
            pm = _PCT.search(u)
            frac = float(pm.group(1)) / 100 if pm else None
        if frac is None:
            return None, "say how much to sell, for example half, 25 percent, or all"
        if coins is None:
            if len(open_numbers) == 1:
                coins = list(open_numbers)
            else:
                return None, "say which coin, for example coin 1, or say sell all"
        return PositionAction("sell", coins, fraction=frac, ticker=ticker), None

    if _BE.search(u) and (_STOP.search(u) or _MOVE.search(u) or "stop" in u):
        if coins is None:
            coins = "all" if len(open_numbers) != 1 else list(open_numbers)
        return PositionAction("set_stop", coins, break_even=True, ticker=ticker), None

    if _STOP.search(u) and _PCT.search(u):
        pct = float(_PCT.search(u).group(1))
        if coins is None:
            coins = "all" if len(open_numbers) != 1 else list(open_numbers)
        return PositionAction("set_stop", coins, stop_loss_pct=pct, ticker=ticker), None

    if _TP.search(u) and _PCT.search(u):
        pct = float(_PCT.search(u).group(1))
        if coins is None:
            coins = "all" if len(open_numbers) != 1 else list(open_numbers)
        return PositionAction("set_tp", coins, take_profit_pct=pct, ticker=ticker), None

    return None, "say what you want to change, for example sell half of coin 1, or move my stop to break-even"


async def extract_position_action_llm(utterance: str, open_numbers: list[int],
                                      fallback_why: str | None = None,
                                      tickers: dict[str, int] | None = None) -> tuple[PositionAction | None, str | None]:
    user = "Open positions: " + (", ".join("coin " + str(n) for n in open_numbers) or "none")
    if tickers:
        user += ". Tickers: " + ", ".join(tickers)
    user += ".\nUser: " + utterance
    try:
        raw = await chat_json(EXTRACT_SYSTEM, user, timeout=8)
    except LLMError:
        return None, fallback_why or "I could not read that action"
    return _from_llm(raw, open_numbers, fallback_why)


def _from_llm(raw: dict, open_numbers: list[int], fallback_why: str | None) -> tuple[PositionAction | None, str | None]:
    kind = raw.get("kind")
    if kind not in ("sell", "set_stop", "set_tp"):
        return None, raw.get("why") or fallback_why or "say which position and what to do"
    coins = raw.get("coins")
    if coins == "all":
        resolved: list[int] | str = "all"
    else:
        nums = [int(x) for x in (coins or []) if str(x).isdigit() and int(x) in open_numbers]
        if not nums:
            if len(open_numbers) == 1:
                resolved = list(open_numbers)
            else:
                return None, "say which coin, for example coin 1"
        else:
            resolved = nums
    if kind == "sell":
        frac = raw.get("fraction")
        try:
            frac = float(frac)
        except (TypeError, ValueError):
            return None, "say how much to sell"
        if not (0 < frac <= 1):
            return None, "the sell size must be between 0 and 100 percent"
        return PositionAction("sell", resolved, fraction=frac, ticker=(raw.get("ticker") or None)), None
    if kind == "set_stop":
        be = bool(raw.get("break_even"))
        pct = raw.get("stop_loss_pct")
        if be:
            return PositionAction("set_stop", resolved, break_even=True), None
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            return None, "say the new stop as a percent"
        if not (0 <= pct <= 90):
            return None, "that stop loss is outside the allowed range"
        return PositionAction("set_stop", resolved, stop_loss_pct=pct), None
    try:
        tp = float(raw.get("take_profit_pct"))
    except (TypeError, ValueError):
        return None, "say the new take profit as a percent"
    if not (0.1 <= tp <= 1000):
        return None, "that take profit is outside the allowed range"
    return PositionAction("set_tp", resolved, take_profit_pct=tp), None


async def resolve_position_action(utterance: str | None, open_numbers: list[int],
                                  tickers: dict[str, int] | None = None) -> tuple[PositionAction | None, str | None]:
    heard, why = parse_position_action(utterance, open_numbers, tickers)
    if heard is not None:
        return heard, None
    if not (utterance or "").strip():
        return None, why or "I did not get your exact words"
    return await extract_position_action_llm(utterance or "", open_numbers, fallback_why=why, tickers=tickers)
