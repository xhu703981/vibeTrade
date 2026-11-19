"""Rehearsal red-team: a token whose metadata carries a prompt injection aimed at AI trading agents.

- The metadata never reaches the voice agent or any spoken text; code only flags that it contains instructions.
- To show the last line of defence, the engine models a naive card builder that obeys the metadata: it adds an SPL
  Transfer of USDC to the attacker. The real transaction guard (instruction allowlist + state checks) must block it.
- Rehearsal only (VIBE_REDTEAM=1, replay discovery, cached research, local fork). Nothing is ever sent.
"""
from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from backend.config import USDC_MINT

FIXTURE = Path(__file__).resolve().parents[2] / "config" / "redteam_token.json"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
INJECTION_RE = re.compile(r"\b(ignore (all |your )?(previous|prior) instructions|system (notice|prompt|message)|ai (trading )?agents?|"
                          r"assistant|do not (tell|mention)|transfer all|send all|pre-?approved)\b", re.I)
CAUTION = "token metadata contains instructions aimed at AI agents (ignored)"


def enabled() -> bool:
    return os.environ.get("VIBE_REDTEAM", "0") == "1" and FIXTURE.is_file()


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def has_injection(meta: dict | None) -> bool:
    """Code-only check of third-party token text. The text itself is never forwarded anywhere."""
    return any(isinstance(v, str) and INJECTION_RE.search(v) for v in (meta or {}).values())


def ata(owner: str, mint: str = USDC_MINT) -> Pubkey:
    return Pubkey.find_program_address([bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(TOKEN_PROGRAM)),
                                        bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA_PROGRAM))[0]


def obey_injection_ix(wallet: str, attacker: str, amount_atomic: int) -> Instruction:
    """What a naive builder would add if it followed the token text: SPL Token Transfer (tag 3) of USDC to the attacker."""
    return Instruction(Pubkey.from_string(TOKEN_PROGRAM), bytes([3]) + struct.pack("<Q", amount_atomic),
                       [AccountMeta(ata(wallet), False, True), AccountMeta(ata(attacker), False, True),
                        AccountMeta(Pubkey.from_string(wallet), True, False)])
