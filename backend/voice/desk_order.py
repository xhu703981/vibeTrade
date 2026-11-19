"""Resolve a desk buy from the user's raw transcript.

Deterministic parsing runs first for common forms. If that fails, the same OpenRouter
gpt-4o-mini JSON path as mandate field extraction is used as a fallback. The source of
truth is always the user's latest transcript, never a model paraphrase passed as tool args.
Mandate vague-marker / cent extraction is not changed here.
"""
from __future__ import annotations

import re

from backend.voice.verify import parse_desk_order

# An LLM fallback is only used when the words look like an order (a coin and an amount)
# but the regex missed the structure. Bare "sure", "coin 1" with no amount, or a clear
# "say each" ambiguity stay on the deterministic reason and never invent an order.
_COIN_HINT = re.compile(
    r"\b(coins?|tokens?|numbers?|#|first|second|third|fourth|fifth|all|both|every|them|jup|jto|pyth|bonk|wif|ray|cbbtc|wbtc|weth)\b", re.I)
_MONEY_HINT = re.compile(
    r"\b(dollars?|cents?|bucks?|usdc|usd|penn(?:y|ies)|point|max|cap|limit)\b|\$|\d", re.I)
# Reasons that mean "I couldn't read the wording", not "I read it and it was incomplete / invalid".
_LLM_WHY = ("did not hear an amount", "say which coin", "I did not hear", "did not get your exact words")

EXTRACT_SYSTEM = """You extract buy orders from the user's exact words for a coin selection desk.
Open coins on the desk: {open_coins}. Per-trade cap: {cap:g} dollars.
Return strict JSON only, with no explanation:
{{"orders": [{{"coin_number": 1, "amount_usdc": 0.01}}]}}
If you cannot extract a complete order from these words, return {{"orders": [], "reason": "short reason"}}.

Rules:
- Use only the user's words. Do not invent a coin or amount they did not say. Do not use earlier conversation or treat "yes"/"sure" as an amount.
- "coin one" / "coin 1" / "the first one" / "number 1" are coin numbers. "all of them" / "both" means every open coin.
- Amounts in dollars: "0.01 dollars", "zero point zero one dollars", "one cent", "1 cent", "$0.01", "a penny" are 0.01; "five cents" is 0.05; "five dollars" is 5. "the max" / "the cap" / "the full amount" is the per-trade cap.
- "five cents each" with several coins applies that amount to each named (or all) coins.
- If the words are not an order, or a coin or amount is missing, return empty orders and a short reason.
"""


def fold_major_tickers(utterance: str | None, tickers: dict[str, int] | None) -> str:
    """Replace allowlisted tickers (JUP, ...) with the majors desk coin number so the usual parser can run."""
    u = utterance or ""
    for name, num in sorted((tickers or {}).items(), key=lambda kv: -len(kv[0])):
        u = re.sub(r"\b" + re.escape(name) + r"\b", "coin " + str(num), u, flags=re.I)
    return u


async def resolve_desk_order(utterance: str | None, open_numbers: list[int],
                             cap_usdc: float, tickers: dict[str, int] | None = None) -> tuple[dict[int, float] | None, str | None]:
    """Parse the user's words into {coin_number: amount_usdc}. Deterministic first, LLM fallback."""
    utterance = fold_major_tickers(utterance, tickers)
    heard, why = parse_desk_order(utterance, open_numbers, cap_usdc)
    if heard is not None:
        return heard, None
    if not (utterance or "").strip():
        return None, why or "I did not get your exact words"
    u = utterance or ""
    if not (why and any(s in why for s in _LLM_WHY) and _COIN_HINT.search(u) and _MONEY_HINT.search(u)):
        return None, why or "say which coin and how much, for example \"coin 1, 1 cent\""
    return await extract_desk_order_llm(utterance, open_numbers, cap_usdc, fallback_why=why)


async def extract_desk_order_llm(utterance: str, open_numbers: list[int], cap_usdc: float,
                                 fallback_why: str | None = None) -> tuple[dict[int, float] | None, str | None]:
    """LLM extraction of desk orders from the user's exact words. Returns (orders, None) or (None, reason)."""
    import os

    from backend.config import settings
    from backend.judge.llm import LLMError, chat_json

    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("VIBE_DESK_LLM"):
        return None, fallback_why or "I did not hear a complete order"
    if not settings.llm_api_key:
        return None, fallback_why or "I did not hear a complete order"
    system = EXTRACT_SYSTEM.format(
        open_coins=", ".join(f"coin {n}" for n in open_numbers) or "(none)",
        cap=float(cap_usdc),
    )
    try:
        data = await chat_json(system, f"User's exact words: {utterance}")
    except LLMError:
        return None, fallback_why or "I did not hear a complete order"
    return _orders_from_llm(data, fallback_why)


def _orders_from_llm(data: dict, fallback_why: str | None) -> tuple[dict[int, float] | None, str | None]:
    raw = data.get("orders") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        reason = ""
        if isinstance(data, dict):
            reason = str(data.get("reason") or "").strip()
        return None, (reason if reason and len(reason) < 160 else None) or fallback_why or "I did not hear a complete order"
    out: dict[int, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None, fallback_why or "I did not hear a complete order"
        try:
            n = int(item.get("coin_number"))
            amt = round(float(item.get("amount_usdc")), 6)
        except (TypeError, ValueError):
            return None, fallback_why or "I did not hear a complete order"
        if n <= 0 or amt <= 0 or n in out:
            return None, fallback_why or "I did not hear a complete order"
        out[n] = amt
    return out, None
