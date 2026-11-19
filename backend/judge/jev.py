"""JEV client: TypeSafe's decision model (OpenRouter /api/alpha/decisions), returning a calibrated P(true).

Ported from the old EVM guard's guard/judge.py: same endpoint, same noul question format, same threshold idea.
Principle: JEV can only escalate (ask again / hand to a human) and never loosens deterministic rules.
"""
from __future__ import annotations

import json

import httpx

from backend.config import settings

JEV_MODEL = "typesafe/jev-1.14"
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
EXPLICIT_THRESHOLD = 0.5   # P(explicit) below this -> treated as vague, ask again


class JEVError(RuntimeError):
    pass


class JEVClient:
    def __init__(self, api_key: str | None = None, model: str = JEV_MODEL, timeout: float = 30.0):
        self._key = api_key or settings.jev_api_key
        self.model = model
        self.timeout = timeout

    async def ask(self, state: str, name: str, question: dict) -> float:
        """One JEV decision call with a single noul question. Returns P(true)."""
        if not self._key:
            raise JEVError("JEV_API_KEY is not set")
        payload = {"model": self.model, "state": state, "questions": {name: question}}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(JEV_URL, headers={"Authorization": f"Bearer {self._key}"}, json=payload)
        if r.status_code != 200:
            raise JEVError(f"JEV HTTP {r.status_code}: {r.text[:300]}")
        return float(r.json()["answers"][name]["noul"])

    async def explicit(self, agent_question: str, utterance: str, field_label: str, value: object) -> float:
        """P(the user explicitly and concretely gave this value for the field). State holds only the user's words and our own system text."""
        state = (
            "The trading agent asked the user (system text, trusted): " + json.dumps(agent_question, ensure_ascii=False)
            + "\nUser's own words: " + json.dumps(utterance, ensure_ascii=False)
            + "\nField being set: " + json.dumps(field_label, ensure_ascii=False)
            + "\nValue the extractor read from the user's words: " + json.dumps(value, ensure_ascii=False)
        )
        return await self.ask(state, "explicit", {
            "type": "noul",
            "instructions": (
                "A voice trading agent is filling a risk mandate that will limit what it may do with the "
                "user's money. Did the user explicitly and unambiguously state this value for this field, "
                "so it can be written into the mandate without guessing? Hedged or delegating wording such as "
                "'maybe', 'about', 'around', 'roughly', 'something like', 'whatever you think', 'you decide', 'default is fine', "
                "'差不多', '大概', '左右', '随便', '你看着办', '你定', or a value the "
                "extractor had to infer, does NOT count as explicit. A clear number or a clear yes/no from the "
                "user, even phrased casually, counts as explicit. Spoken fillers such as 'I guess', 'I think', 'um', "
                "'uh', 'let's just', 'let's say', 'for this time' around a clear number are NOT hedges. A short bare answer "
                "(e.g. '30%' or 'five cents') to the question the agent just asked counts as explicit for the field that "
                "question asked about. Values written in another unit by code from the user's words (cents as dollars, "
                "'take profit 30%' as a 30 percent gain) count as explicit when the words are clear."
            ),
            "criteria": {
                "true": "The user clearly stated this exact value for this field; no hedging, no delegation, no guessing needed.",
                "false": "The value is hedged, approximate, delegated to the agent, or not actually stated by the user.",
            },
        })
