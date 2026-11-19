"""Saved default mandate used to skip the long first-time setup.

The file (config/default_mandate.json, overridable with VIBE_DEFAULT_MANDATE) is merged
on top of schema defaults. Coin filters and everything not listed stay at current defaults.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

from backend.mandate.schema import MandateBody, default_body

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "default_mandate.json"


def preset_path() -> Path:
    env = os.environ.get("VIBE_DEFAULT_MANDATE")
    return Path(env) if env else DEFAULT_PATH


def _merge(base: dict, overlay: dict) -> dict:
    out = deepcopy(base)
    for k, v in overlay.items():
        if k == "funding":
            continue  # wallets stay at schema / runtime defaults
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def load_preset(path: Path | None = None) -> dict:
    """Return a complete mandate body dict (schema defaults + file overlays)."""
    p = path or preset_path()
    overlay: dict = {}
    if p.is_file():
        overlay = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(overlay, dict):
            overlay = {}
    body = _merge(default_body().model_dump(mode="json"), overlay)
    return MandateBody.model_validate(body).model_dump(mode="json")


_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
          "seventeen eighteen nineteen twenty").split()


def money_spoken(usd: float) -> str:
    """'five cents', 'one cent', 'five dollars', '2.5 dollars' (spelled out so speech reads it calmly)."""
    cents = round(usd * 100)
    if usd < 1 and abs(usd * 100 - cents) < 1e-6:
        n = _WORDS[cents] if cents < len(_WORDS) else str(cents)
        return n + (" cent" if cents == 1 else " cents")
    if abs(usd - round(usd)) < 1e-9:
        n = int(round(usd))
        w = _WORDS[n] if n < len(_WORDS) else str(n)
        return w + (" dollar" if n == 1 else " dollars")
    return f"{usd:g} dollars"


def greeting_text(body: dict) -> str:
    """The agent's first line, built from the saved settings (never hard-coded numbers)."""
    b = body.get("budget") or {}
    parts = []
    if b.get("total_usdc"):
        parts.append(money_spoken(float(b["total_usdc"])) + " total")
    if b.get("per_trade_usdc"):
        parts.append(money_spoken(float(b["per_trade_usdc"])) + " per trade")
    if b.get("daily_max_loss_usdc"):
        parts.append("stop buying after a " + money_spoken(float(b["daily_max_loss_usdc"])).replace("dollars", "dollar")
                     .replace("cents", "cent") + " loss today")
    if not parts:
        said = "the saved settings"
    elif len(parts) == 1:
        said = parts[0]
    else:
        said = ", ".join(parts)
    return f"Hi, I'm Vibe-Trader. Same as last time: {said}?"
