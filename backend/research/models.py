"""Seller responses -> pydantic models with structured fields only. Everything a seller returns is untrusted:

- names, symbols, labels (address_label), links and logos are dropped;
- addresses must be valid base58 public keys, numbers finite, booleans real bools;
- free text (Nansen warnings, Quick Intel projectVerifiDescription) is truncated into `untrusted_text`
  and only passed to JEV as data (can only tighten); it never enters trading prompts, voice or panels.
"""
from __future__ import annotations

import math
import re

from pydantic import BaseModel, Field

from backend.discovery.dexscreener import valid_mint

QI_RISK_FLAGS = ("can_Mint", "can_Mint_Bridge", "can_Freeze_Trading", "default_State_Frozen", "can_Update_Fees",
                 "has_Delegated_Ownership", "has_External_Contract_Risk")
QI_POOL_TYPES = {"PUMPSWAP", "RAYDIUM", "RAYDIUM_CPMM", "CPMM", "METEORA", "ORCA", "PUMPFUN"}
_CTRL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _num(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float, str)):
        return None
    try:
        v = float(x)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _bool(x) -> bool | None:
    return x if isinstance(x, bool) else None


def _text(x, n: int = 300) -> str | None:
    if not isinstance(x, str) or not x.strip():
        return None
    return _CTRL.sub(" ", x).strip()[:n]


class HolderRow(BaseModel):
    address: str
    token_amount: float = Field(ge=0)


class NansenHolders(BaseModel):
    rows: list[HolderRow]
    is_last_page: bool | None
    untrusted_text: list[str] = []


def parse_nansen_holders(d) -> NansenHolders | None:
    if not isinstance(d, dict) or not isinstance(d.get("data"), list):
        return None
    rows = []
    for h in d["data"][:1000]:
        if not isinstance(h, dict):
            continue
        a, amt = valid_mint(h.get("address")), _num(h.get("token_amount"))
        if a and amt is not None and amt >= 0:
            rows.append(HolderRow(address=a, token_amount=amt))
    pg = d.get("pagination") if isinstance(d.get("pagination"), dict) else {}
    warn = [t for t in (_text(w) for w in (d.get("warnings") or [])[:5] if isinstance(d.get("warnings"), list)) if t]
    return NansenHolders(rows=rows, is_last_page=_bool(pg.get("is_last_page")), untrusted_text=warn)


class QuickIntelScan(BaseModel):
    contract_address: str | None
    chain: str | None
    creator: str | None
    honeypot: bool | None
    is_scam: bool | None
    has_scams: bool | None
    airdrop_phishing: bool | None
    risk_flags: dict[str, bool | None]
    lp_burned_pct: float | None
    pool_type: str | None
    untrusted_text: list[str] = []


def parse_quickintel(d) -> QuickIntelScan | None:
    if not isinstance(d, dict) or not isinstance(d.get("quickiAudit"), dict):
        return None
    au, dyn = d["quickiAudit"], d.get("tokenDynamicDetails") if isinstance(d.get("tokenDynamicDetails"), dict) else {}
    chain = au.get("contract_Chain")
    pt = dyn.get("pool_Type")
    txt = [t for t in (_text(d.get("projectVerifiDescription")),) if t]
    return QuickIntelScan(
        contract_address=valid_mint(au.get("contract_Address")),
        chain=chain if chain in ("solana",) else None,
        creator=valid_mint(au.get("contract_Creator")),
        honeypot=_bool(dyn.get("is_Honeypot")),
        is_scam=_bool(d.get("isScam")),
        has_scams=_bool(au.get("has_Scams")),
        airdrop_phishing=_bool(d.get("isAirdropPhishingScam")),
        risk_flags={k: _bool(au.get(k)) for k in QI_RISK_FLAGS},
        lp_burned_pct=_num(dyn.get("lp_Burned_Percent")),
        pool_type=pt if isinstance(pt, str) and pt in QI_POOL_TYPES else None,
        untrusted_text=txt,
    )
