"""Dialogue LLM (OpenRouter chat completions, default openai/gpt-4o-mini, same as the old intent.py)."""
from __future__ import annotations

import json

import httpx

from backend.config import settings

LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = "openai/gpt-4o-mini"


class LLMError(RuntimeError):
    pass


async def chat_json(system: str, user: str, model: str = LLM_MODEL, api_key: str | None = None, timeout: float = 60.0) -> dict:
    key = api_key or settings.llm_api_key
    if not key:
        raise LLMError("LLM_API_KEY is not set")
    body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(LLM_URL, headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code != 200:
        raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
    try:
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError) as e:
        raise LLMError(f"LLM did not return JSON: {e}") from e
