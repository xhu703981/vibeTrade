"""Cache of purchased research data (never pay the same seller twice for the same coin in a round). Raw responses live only in runs/ (gitignored)."""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "runs" / "research_cache"
TTL_S = 6 * 3600


def _path(seller: str, mint: str) -> Path:
    return ROOT / f"{seller}_{mint}.json"


def get(seller: str, mint: str, ttl_s: float | None = None) -> dict | None:
    ttl_s = TTL_S if ttl_s is None else ttl_s   # read TTL_S at runtime: cache-replay mode raises it
    p = _path(seller, mint)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):   # unreadable or half-written file (UnicodeDecodeError is a ValueError): a miss, not a crash
        return None
    if not isinstance(d, dict):
        return None
    return d if time.time() - d.get("ts", 0) <= ttl_s else None


def put(seller: str, mint: str, data, tx_hash: str | None, amount_usdc: float | None = None) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"seller": seller, "mint": mint, "ts": time.time(), "tx_hash": tx_hash,
                       "amount_usdc": amount_usdc, "data": data}, ensure_ascii=False)
    # UTF-8 always (token names carry emoji; Windows' default code page can't encode them), written atomically so a
    # failed write never leaves an empty file behind
    p = _path(seller, mint)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
