"""Co-pilot desk helpers: rank coins, write a one-sentence reason, risk-based sizing, JEV suspicion questions.

Everything spoken or shown here is written by code from numbers; no token name or third-party text is used.
"""
from __future__ import annotations

import asyncio
import json
import re

from backend.voice.verify import amounts_in

TOP_N = 3                      # the desk recommends at most this many coins per round
SUSPICION_ASK = 0.6            # the agent asks about a coin only when its most suspicious item is at least this likely


def _num(m: dict, k: str):
    v = m.get(k)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def rank_score(m: dict) -> float:
    """Higher = better short-term setup: buy pressure, steady (not parabolic) move, spread-out holders, cheap round trip."""
    s = 0.0
    sm = _num(m, "sm_netflow_24h_usd")
    if sm is not None and sm > 0:
        import math
        s += min(2.0, math.log10(1 + sm) / 2.5)          # smart money net buying is the strongest signal we pay for
    b, sl = _num(m, "buys_h1"), _num(m, "sells_h1")
    if b is not None and sl is not None and b + sl > 0:
        s += 2.0 * (b - sl) / (b + sl)
    h1, h24 = _num(m, "price_change_h1"), _num(m, "price_change_h24")
    if h1 is not None:
        s += max(-1.0, min(1.0, h1 / 10))
    if h24 is not None and h24 >= 100:
        s -= 1.5                                   # chasing a pump
    t10 = _num(m, "top10_pct")
    if t10 is not None:
        s += (40 - t10) / 40
    vl = _num(m, "volume_to_liquidity")
    if vl is not None:
        s += 0.5 if 0.5 <= vl <= 8 else -0.5 if vl > 12 else 0.0
    rt = _num(m, "roundtrip_loss_pct")
    if rt is not None:
        s -= rt / 5
    return round(s, 4)


def _usd_short(x: float) -> str:
    """$5.1k / $44k / $1.2M: the on-screen money form (the spoken form comes from spoken())."""
    a = abs(x)
    if a >= 1_000_000:
        return f"${a / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 10_000:
        return f"${a / 1_000:.0f}k"
    if a >= 1_000:
        return f"${a / 1_000:.1f}k".replace(".0k", "k")
    return f"${a:.0f}"


def _times(r: float) -> str:
    return f"{r:.0f}\u00d7" if r >= 3 else f"{r:.1f}\u00d7".replace(".0\u00d7", "\u00d7")


# ---------- limits: a figure is only a concern near or over the mandate's own limit ----------
NEAR_LIMIT = 0.875             # "near the limit" = at least 87.5% of it (35% when the top-10 limit is 40%)


def limits(f=None) -> dict[str, float]:
    """The mandate's limit for each limit-based figure; the schema defaults when no mandate filters are given."""
    from backend.mandate.schema import Filters
    f = f if f is not None else Filters()
    return {"top10_pct": float(f.max_top10_pct), "dev_pct": float(f.max_dev_pct),
            "volume_to_liquidity": float(f.max_volume_to_liquidity)}


def near_limit(value: float | None, limit: float | None) -> bool:
    return value is not None and limit is not None and limit > 0 and value >= NEAR_LIMIT * limit


def vs_limit(value: float, limit: float, unit: str = "%") -> str:
    """'under your 40% limit' / 'near your 40% limit' / 'over your 40% limit' (always the comparison, never a label)."""
    lim = f"{limit:g}{unit}"
    if value > limit:
        return f"over your {lim} limit"
    return f"near your {lim} limit" if near_limit(value, limit) else f"under your {lim} limit"


_COUNT = {2: "two", 3: "three", 4: "four", 5: "five"}


def holder_note(m: dict, peers: list[dict] | None = None, f=None) -> str | None:
    """The top-10 share said with what it is compared to: 'Top 10 wallets hold 28%, under your 40% limit, highest of
    the three picks'. None when the share is unknown."""
    t10 = _num(m, "top10_pct")
    if t10 is None:
        return None
    lim = limits(f)["top10_pct"]
    out = f"Top 10 wallets hold {t10:.0f}%, {vs_limit(round(t10), lim)}"
    vals = [v for v in (_num(p, "top10_pct") for p in (peers or [])) if v is not None]
    if len(vals) >= 2 and len(vals) == len(peers or []):
        word = _COUNT.get(len(vals), str(len(vals)))
        if t10 == max(vals) and vals.count(t10) == 1:
            out += f", highest of the {word} picks"
        elif t10 == min(vals) and vals.count(t10) == 1:
            out += f", lowest of the {word} picks"
    return out


def reason_candidates(m: dict, f=None) -> list[tuple[str, tuple[str, ...]]]:
    """(sentence, its distinguishing number) in order of preference. Every sentence carries a number of this coin's own."""
    out: list[tuple[str, str]] = []
    sm, smt = _num(m, "sm_netflow_24h_usd"), _num(m, "sm_traders")
    if sm is not None and sm >= 500:
        if smt:
            out.append((f"{int(smt)} smart wallets bought {_usd_short(sm)} today", (f"w{int(smt)}", _usd_short(sm))))
        out.append((f"Smart wallets bought {_usd_short(sm)} today", (_usd_short(sm),)))
    v1, v24 = _num(m, "volume_h1"), _num(m, "volume_h24")
    if v1 is not None and v24 and v24 > 0:
        r = v1 / (v24 / 24)
        if r >= 2:
            out.append((f"Volume up {_times(r)} in the last hour", (_times(r),)))
    b, sl = _num(m, "buys_h1"), _num(m, "sells_h1")
    if b is not None and sl is not None and b + sl >= 20 and b > 1.3 * sl:
        out.append((f"{int(b)} buys vs {int(sl)} sells in the last hour", (f"b{int(b)}",)))
    h1 = _num(m, "price_change_h1")
    if h1 is not None and 2 <= h1 <= 25:
        out.append((f"Up {h1:.0f}% in the last hour", (f"h{h1:.0f}",)))
    t10 = _num(m, "top10_pct")
    if t10 is not None and t10 <= 20:      # a concentration reason always says what it is compared to
        out.append((f"Top 10 wallets hold {t10:.0f}%, {vs_limit(round(t10), limits(f)['top10_pct'])}", (f"t{t10:.0f}",)))
    hold = _num(m, "holders")
    if hold is not None:
        out.append((f"{int(hold):,} holders", (f"n{int(hold)}",)))
    vl = _num(m, "volume_to_liquidity")
    if vl is not None and 0.5 <= vl <= 8:
        out.append((f"Traded {vl:.1f}\u00d7 its liquidity today", (f"v{vl:.1f}",)))
    return out


def desk_reasons(metrics: list[dict], f=None) -> list[str]:
    """One short line per coin on why it made the shortlist. Each line carries a number unique to that coin on this
    desk (two coins never get the same headline number); the source (e.g. Nansen) lives in the coin's details."""
    used: set[str] = set()
    out = []
    for m in metrics:
        cands = reason_candidates(m, f)
        pick = next((c for c in cands if not used.intersection(c[1])), cands[0] if cands else None)
        if pick is None:
            out.append("Passed every safety check")
            continue
        used.update(pick[1])
        out.append(pick[0])
    return out


def desk_reason(m: dict) -> str:
    return desk_reasons([m])[0]


def warning_text(key: str | None, m: dict, f=None) -> str | None:
    """The concern as a concrete number of this coin and the mandate limit it is near or over ("Top 10 wallets hold
    36%, near your 40% limit"), or None when no number is itself a concern. Limit-based figures (top-10 share, dev
    holding, turnover) count only at NEAR_LIMIT of the mandate's own limit or above: 28% under a 40% limit is no warning,
    whatever the model's score."""
    lim = limits(f)
    if key == "dump":
        t10, dev = _num(m, "top10_pct"), _num(m, "dev_pct")
        if near_limit(t10, lim["top10_pct"]):
            return f"Top 10 wallets hold {t10:.0f}%, {vs_limit(round(t10), lim['top10_pct'])}"
        if near_limit(dev, lim["dev_pct"]):
            return f"Dev wallet holds {dev:.1f}%".replace(".0%", "%") + f", {vs_limit(dev, lim['dev_pct'])}"
    if key == "pump":
        h1, h24 = _num(m, "price_change_h1"), _num(m, "price_change_h24")
        b, sl = _num(m, "buys_h1"), _num(m, "sells_h1")
        if h1 is not None and h1 >= 5:
            return f"Up {h1:.0f}% in the last hour"
        if h24 is not None and h24 >= 50:
            return f"Up {h24:.0f}% in 24 hours"
        if b is not None and sl is not None and b >= 2 * max(sl, 1):
            return f"{int(b)} buys vs {int(sl)} sells in the last hour"
    if key == "wash":
        vl = _num(m, "volume_to_liquidity")
        if near_limit(vl, lim["volume_to_liquidity"]):
            return f"Traded {vl:.0f}\u00d7 its liquidity in 24 hours, {vs_limit(vl, lim['volume_to_liquidity'], unit=chr(0xd7))}"
    return None


def spoken(text: str) -> str:
    """The on-screen line in words the voice reads the same way: $5.1k -> about 5.1 thousand dollars, 3x -> 3 times, 28% -> 28 percent."""
    def money(mo: re.Match) -> str:
        n, unit = mo.group(1), mo.group(2)
        word = {"k": " thousand", "M": " million", "": ""}[unit]
        return f"about {n}{word} dollars" if unit else f"{n} dollars"
    t = re.sub(r"\$(\d+(?:\.\d+)?)(k|M)?", money, text)
    t = t.replace("\u00d7", " times").replace("%", " percent").replace("\u2212", "minus ")
    return t


# ---------- JEV: two or three specific numeric yes/no questions per top coin ----------
def suspicion_questions(m: dict) -> list[tuple[str, str, str, dict]]:
    """(key, label shown on the card, state, question). Only questions whose numbers are known are asked."""
    out = []
    g = lambda k: _num(m, k)  # noqa: E731
    if g("price_change_h1") is not None and g("buys_h1") is not None and g("sells_h1") is not None:
        state = json.dumps({"price_change_1h_pct": g("price_change_h1"), "price_change_24h_pct": g("price_change_h24"),
                            "buys_1h": g("buys_h1"), "sells_1h": g("sells_h1"), "liquidity_usd": g("liquidity_usd")})
        out.append(("pump", "the last hour looks like a coordinated pump", "Solana meme coin market numbers: " + state, {
            "type": "noul",
            "instructions": "From these numbers alone: is the last hour's move more likely a short coordinated pump that "
                            "reverses within hours than organic buying?",
            "criteria": {"true": "Likely a short coordinated pump.", "false": "Looks like ordinary organic trading."}}))
    if g("top10_pct") is not None:
        state = json.dumps({"top10_holders_pct": g("top10_pct"), "dev_wallet_pct": g("dev_pct"), "holders": g("holders"),
                            "liquidity_usd": g("liquidity_usd"), "market_cap_usd": g("market_cap")})
        out.append(("dump", "a few wallets could dump it", "Solana meme coin holder numbers: " + state, {
            "type": "noul",
            "instructions": "From these numbers alone: could the top holders sell enough, at this liquidity, to drop the "
                            "price by 20 percent or more within an hour?",
            "criteria": {"true": "Yes, a few wallets could crash it.", "false": "No, holdings are spread out enough."}}))
    if g("volume_to_liquidity") is not None and g("buys_h24") is not None and g("sells_h24") is not None:
        state = json.dumps({"volume_24h_over_liquidity": g("volume_to_liquidity"), "buys_24h": g("buys_h24"),
                            "sells_24h": g("sells_h24"), "volume_24h_usd": g("volume_h24")})
        out.append(("wash", "volume may be wash trading", "Solana meme coin trading numbers: " + state, {
            "type": "noul",
            "instructions": "From these numbers alone: is most of this volume likely wash trading (the same wallets "
                            "trading back and forth) rather than real buyers and sellers?",
            "criteria": {"true": "Likely mostly wash trading.", "false": "Likely real trading."}}))
    return out[:3]


async def suspicion(jev, m: dict, timeout: float = 25.0) -> dict | None:
    """Ask JEV the coin's questions in parallel; returns the most suspicious item {key, label, score, all}. None if unavailable."""
    qs = suspicion_questions(m)
    ask = getattr(jev, "ask", None)
    if not qs or not callable(ask):
        return None

    async def one(key, label, state, q):
        try:
            return key, label, float(await ask(state, key, q))
        except Exception:  # noqa: BLE001  JEV down: no suspicion line, never blocks the desk
            return None
    try:
        got = [r for r in await asyncio.wait_for(asyncio.gather(*(one(*q) for q in qs)), timeout) if r]
    except asyncio.TimeoutError:
        return None
    if not got:
        return None
    key, label, score = max(got, key=lambda r: r[2])
    return {"key": key, "label": label, "score": round(score, 2), "all": {k: round(s, 2) for k, _l, s in got}}


# ---------- risk-based sizing ----------
RISK_RE = re.compile(r"\b(lose|losing|risk|risking)\b", re.I)
COIN_RE = re.compile(r"\bcoin\s*(?:number\s*)?(\d{1,3})\b", re.I)


def parse_risk(utterance: str | None) -> tuple[float | None, int | None]:
    """'I'm willing to lose one dollar on this' -> (1.0, None); 'risk 50 cents on coin 2' -> (0.5, 2)."""
    from backend.execute.cards import normalize_utterance
    if not utterance or not RISK_RE.search(utterance):
        return None, None
    u = normalize_utterance(utterance)
    coin = None
    m = COIN_RE.search(u)
    if m:
        coin = int(m[1])
        u = u[:m.start()] + " " + u[m.end():]
    r = RISK_RE.search(u)
    if r:                       # the amount belongs to the risk phrase: "reject 35. I'm willing to lose ten cents" -> 0.10
        u = u[r.start():]
    amts = [a for a in amounts_in(u) if a > 0]
    if not amts or not re.search(r"\$|cents?\b|dollars?\b|bucks?\b|usdc?\b", u, re.I):
        return None, coin
    return amts[0], coin


def size_from_risk(risk: float, stop_pct: float, per_trade: float, left: float) -> tuple[float, str | None]:
    """amount = risk / stop distance, capped by the per-trade cap and what is left. Returns (amount, cap note)."""
    raw = risk / (stop_pct / 100)
    amt = min(raw, per_trade, left)
    note = None
    if amt < raw - 1e-9:
        note = "per-trade cap" if amt == per_trade else "money left today"
    import math
    return math.floor(max(0.0, amt) * 100 + 1e-6) / 100, note      # whole cents, never above a cap


def loss_bounds(amount: float, stop_pct: float, stop_sell_slippage_bps: int, gap_pct: float | None = None) -> tuple[float, float]:
    """(planned, worst) loss in dollars if the stop fires. Planned = amount x stop. Worst = the price gaps gap_pct past
    the stop before the sell, and the sell fills at the full allowed stop-loss slippage."""
    if gap_pct is None:
        from backend.config import STOP_GAP_PCT
        gap_pct = STOP_GAP_PCT
    planned = amount * stop_pct / 100
    left = max(0.0, 1 - (stop_pct + gap_pct) / 100) * (1 - stop_sell_slippage_bps / 10_000)
    return round(planned, 4), round(amount * (1 - left), 4)


CAP_WORDS = {"per-trade cap": "capped at the per-trade limit", "money left today": "capped at what's left to spend"}
