"""Map research data onto M3's pending_research fields and judge pass/fail by mandate thresholds. Missing data = fail."""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.research.models import QI_RISK_FLAGS, NansenHolders, QuickIntelScan
from backend.research.onchain import INCINERATOR

QI_FLAG_LABEL = {"can_Mint": "can mint more supply", "can_Mint_Bridge": "bridge can mint", "can_Freeze_Trading": "can freeze trading",
                 "default_State_Frozen": "new accounts frozen by default", "can_Update_Fees": "can change fees",
                 "has_Delegated_Ownership": "has delegated ownership", "has_External_Contract_Risk": "external contract risk"}


@dataclass
class Facts:
    top10_pct: float | None = None
    dev_pct: float | None = None
    holders: int | None = None
    known_rugger: bool | None = None
    scan_flags: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    untrusted_text_count: int = 0


def top10_pct(h: NansenHolders, supply_ui: float, exclude: set[str]) -> float | None:
    """Top-10 holder share (excluding pool / pool authority / burn addresses), with on-chain total supply as the denominator."""
    if supply_ui <= 0:
        return None
    rows = sorted((r for r in h.rows if r.address not in exclude | {INCINERATOR}), key=lambda r: -r.token_amount)
    if len(rows) < 10 and h.is_last_page is not True:
        return None  # incomplete data
    return sum(r.token_amount for r in rows[:10]) / supply_ui * 100


def scan_problems(q: QuickIntelScan, mint: str) -> tuple[bool | None, list[str], list[str]]:
    """(known_rugger, scan risks, data issues)"""
    data_issues = []
    if q.contract_address != mint:
        data_issues.append("Quick Intel returned data for a different token")
    if q.chain != "solana":
        data_issues.append("Quick Intel returned a chain other than solana")
    flags = []
    if q.honeypot is True:
        flags.append("honeypot (can buy but not sell)")
    elif q.honeypot is None:
        data_issues.append("Quick Intel is missing the honeypot result")
    for k in QI_RISK_FLAGS:
        v = q.risk_flags.get(k)
        if v is True:
            flags.append(QI_FLAG_LABEL[k])
        elif v is None:
            data_issues.append(f"Quick Intel is missing field {k}")
    if q.has_scams is None:
        rugger = None
    else:
        rugger = bool(q.has_scams or q.is_scam is True or q.airdrop_phishing is True)
    return rugger, flags, data_issues


def evaluate(f: Facts, m) -> list[str]:
    fl = m.body.filters
    r = []
    if f.top10_pct is None:
        r.append("missing top-10 holder share")
    elif f.top10_pct > fl.max_top10_pct:
        r.append(f"top-10 holders {f.top10_pct:.1f}% above {fl.max_top10_pct:g}%")
    if f.dev_pct is None:
        r.append("missing dev holding")
    elif f.dev_pct > fl.max_dev_pct:
        r.append(f"dev holding {f.dev_pct:.2f}% above {fl.max_dev_pct:g}%")
    if f.holders is None:
        r.append("missing holder count")
    elif f.holders < fl.min_holders:
        r.append(f"{f.holders} holders, fewer than {fl.min_holders}")
    if fl.reject_known_rugger:
        if f.known_rugger is None:
            r.append("missing known-rugger check")
        elif f.known_rugger:
            r.append("dev / contract flagged as a scam")
    r += [f"security scan: {x}" for x in f.scan_flags]
    return r
