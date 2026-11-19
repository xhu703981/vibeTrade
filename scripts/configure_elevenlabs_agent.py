"""Configure the ElevenLabs Vibe-Trader agent so its client tools / prompt match the backend.

  python scripts/configure_elevenlabs_agent.py --dry-run   # only show what would change
  python scripts/configure_elevenlabs_agent.py             # apply (backs up the current config to secrets/, gitignored)
  python scripts/configure_elevenlabs_agent.py --custom    # revert the voice mode to Custom (realtime_model = null)

Scope: system prompt, first message, language (LANGUAGE in agent_config), the client tools, client_events (adds
client_tool_call etc.), max call duration, auth enabled (only backend-issued signed URLs can connect), and turn
timing so the agent never re-engages the user on its own (turn_timeout disabled / maxed, soft timeout off), patient
turn eagerness, and the voice mode: conversation_config.realtime_model = REALTIME_MODEL (Eleven Realtime).
The stored voice, TTS model, LLM and ASR are left untouched (Realtime ignores them; Custom mode uses them again), so
--custom reverts exactly. If the workspace rejects Realtime, the agent stays in Custom mode, the other settings are
still applied, and the exact API error is printed. The API key is read from the environment / .env and never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from backend.config import settings  # noqa: E402
from backend.voice.agent_config import (CLIENT_EVENTS_REQUIRED, FIRST_MESSAGE, GREETING_PLACEHOLDER, LANGUAGE, MAX_DURATION_S,  # noqa: E402
                                        REALTIME_MIN_MAX_TOKENS, REALTIME_MODEL, SOFT_TIMEOUT_S, SPECULATIVE_TURN,
                                        SYSTEM_PROMPT, TOOLS, TTS_EXPRESSIVE_MODE, TTS_STABILITY,
                                        TURN_EAGERNESS, TURN_TIMEOUT_CANDIDATES)

API = "https://api.elevenlabs.io/v1/convai"


def client_tool(t: dict) -> dict:
    # immediate + expects_response: every tool answers in well under a second (long work is backgrounded by the backend),
    # so the agent always hears ok=false reasons; interruptions stay allowed around every tool call (duplex).
    # pre_tool_speech off: Realtime otherwise speaks a guessed outcome before the tool returns (seen in the probe).
    return {"type": "client", "name": t["name"], "description": t["description"], "parameters": t["parameters"],
            "expects_response": True, "response_timeout_secs": t["response_timeout_secs"], "execution_mode": "immediate",
            "pre_tool_speech": t.get("pre_tool_speech", "off"), "interruption_mode": "allow"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--custom", action="store_true", help="voice mode Custom (realtime_model = null): the stored LLM / TTS / ASR are used again")
    a = ap.parse_args()
    want_rt = None if a.custom else REALTIME_MODEL
    if not settings.elevenlabs_api_key or not settings.elevenlabs_agent_id:
        sys.exit("ELEVENLABS_API_KEY / ELEVENLABS_AGENT_ID is not set")
    h = {"xi-api-key": settings.elevenlabs_api_key}
    aid = settings.elevenlabs_agent_id
    c = httpx.Client(timeout=30, headers=h)
    r = c.get(f"{API}/agents/{aid}")
    r.raise_for_status()
    agent = r.json()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "secrets" / f"elevenlabs_agent_backup_{ts}.json"
    if not a.dry_run:
        backup.parent.mkdir(exist_ok=True)
        backup.write_text(json.dumps(agent, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"current config backed up to: {backup.relative_to(ROOT)}")

    # 1. tools: look up by name; update if present, create otherwise
    existing = {}
    cursor = None
    while True:
        rr = c.get(f"{API}/tools", params={"cursor": cursor} if cursor else None)
        rr.raise_for_status()
        d = rr.json()
        for t in d.get("tools", []):
            cfg = t.get("tool_config") or {}
            if cfg.get("type") == "client":
                existing[cfg.get("name")] = t["id"]
        cursor = d.get("next_cursor")
        if not d.get("has_more") or not cursor:
            break
    ids = []
    for t in TOOLS:
        body = {"tool_config": client_tool(t)}
        if t["name"] in existing:
            print(f"tool {t['name']}: update {existing[t['name']]}")
            if not a.dry_run:
                c.patch(f"{API}/tools/{existing[t['name']]}", json=body).raise_for_status()
            ids.append(existing[t["name"]])
        else:
            print(f"tool {t['name']}: create")
            if not a.dry_run:
                rr = c.post(f"{API}/tools", json=body)
                if rr.status_code >= 400:
                    sys.exit(f"creating the tool failed: HTTP {rr.status_code}: {rr.text[:400]}")
                ids.append(rr.json()["id"])

    # 2. agent: change only the fields we need
    cc = agent["conversation_config"]
    p = cc["agent"]["prompt"]
    keep_ids = [i for i in (p.get("tool_ids") or []) if i not in existing.values()]
    events = list(dict.fromkeys((cc["conversation"].get("client_events") or []) + CLIENT_EVENTS_REQUIRED))
    patch = {"conversation_config": {
        "agent": {"first_message": FIRST_MESSAGE, "language": LANGUAGE,
                  "dynamic_variables": {"dynamic_variable_placeholders": {"greeting": GREETING_PLACEHOLDER}},
                  "prompt": {"prompt": SYSTEM_PROMPT, "tool_ids": keep_ids + ids}},
        "conversation": {"client_events": events, "max_duration_seconds": MAX_DURATION_S}},
        "platform_settings": {"auth": {"enable_auth": True}}}
    print("\n== agent changes ==")
    print(f"first_message: {cc['agent'].get('first_message')!r} -> {FIRST_MESSAGE!r}")
    print(f"language: {cc['agent'].get('language')} -> {LANGUAGE}")
    print(f"prompt: {len(p.get('prompt') or '')} chars ({(p.get('prompt') or '')[:40]!r}) -> {len(SYSTEM_PROMPT)} chars (see backend/voice/agent_config.py)")
    print(f"tool_ids: {len(p.get('tool_ids') or [])} -> {len(keep_ids) + len(TOOLS)}")
    print(f"client_events: adding {[e for e in events if e not in (cc['conversation'].get('client_events') or [])]}")
    print(f"max_duration_seconds: {cc['conversation'].get('max_duration_seconds')} -> {MAX_DURATION_S}")
    print(f"auth.enable_auth: {(agent.get('platform_settings') or {}).get('auth', {}).get('enable_auth')} -> True")
    turn = cc.get("turn") or {}
    print(f"turn_timeout: {turn.get('turn_timeout')} -> first accepted of {list(TURN_TIMEOUT_CANDIDATES)}; "
          f"soft_timeout: {(turn.get('soft_timeout_config') or {}).get('timeout_seconds')} -> {SOFT_TIMEOUT_S}; "
          f"silence_end_call_timeout: {turn.get('silence_end_call_timeout')} (unchanged)")
    print(f"realtime_model: {cc.get('realtime_model')} -> {want_rt}; turn_eagerness: {turn.get('turn_eagerness')} -> {TURN_EAGERNESS}; "
          f"speculative_turn: {turn.get('speculative_turn')} -> {SPECULATIVE_TURN}; "
          f"max_tokens: {p.get('max_tokens')} (Max needs >= {REALTIME_MIN_MAX_TOKENS} or -1)")
    print(f"stored (kept for Custom mode): voice_id, TTS model {cc['tts'].get('model_id')}, LLM {p.get('llm')}, ASR, temperature")
    if a.dry_run:
        return
    rr = c.patch(f"{API}/agents/{aid}", json=patch)
    if rr.status_code >= 400:
        sys.exit(f"updating the agent failed: HTTP {rr.status_code}: {rr.text[:600]}")
    # 2a. voice mode. Max needs a token limit >= 1024 (or -1 = none); never lower an existing larger limit.
    rt_error = None
    if want_rt == "eleven_realtime_v1_max":
        mt = p.get("max_tokens")
        if mt is not None and mt != -1 and mt < REALTIME_MIN_MAX_TOKENS:
            c.patch(f"{API}/agents/{aid}", json={"conversation_config": {"agent": {"prompt": {"max_tokens": REALTIME_MIN_MAX_TOKENS}}}}).raise_for_status()
            print(f"max_tokens raised {mt} -> {REALTIME_MIN_MAX_TOKENS} (required by Realtime Max)")
    rr = c.patch(f"{API}/agents/{aid}", json={"conversation_config": {"realtime_model": want_rt}})
    if rr.status_code >= 400:
        rt_error = f"HTTP {rr.status_code}: {rr.text[:500]}"
        print(f"realtime_model={want_rt} NOT applied ({rt_error}); the agent stays in its current voice mode")
    elif (rr.json().get("conversation_config") or {}).get("realtime_model") != want_rt:
        rt_error = f"accepted but read back as {(rr.json().get('conversation_config') or {}).get('realtime_model')!r}"
        print(f"realtime_model: {rt_error}")
    else:
        print(f"realtime_model applied: {want_rt}")
    # 2b. idle re-engagement off: the agent must stay quiet until a system update arrives or the user speaks
    turn_applied = None
    for tt in TURN_TIMEOUT_CANDIDATES:
        rr = c.patch(f"{API}/agents/{aid}", json={"conversation_config": {"turn": {
            "turn_timeout": tt, "turn_eagerness": TURN_EAGERNESS, "soft_timeout_config": {"timeout_seconds": SOFT_TIMEOUT_S}}}})
        if rr.status_code < 400 and (rr.json().get("conversation_config") or {}).get("turn", {}).get("turn_timeout") == tt:
            turn_applied = tt
            break
        print(f"turn_timeout={tt} not accepted (HTTP {rr.status_code}); trying the next value")
    print(f"turn_timeout applied: {turn_applied}")
    # 2c. speculative_turn off: Realtime otherwise speaks a pre-tool acknowledgement, then restates the tool result
    spec_applied = None
    rr = c.patch(f"{API}/agents/{aid}", json={"conversation_config": {"turn": {"speculative_turn": SPECULATIVE_TURN}}})
    if rr.status_code < 400:
        spec_applied = (rr.json().get("conversation_config") or {}).get("turn", {}).get("speculative_turn")
    else:
        print(f"speculative_turn={SPECULATIVE_TURN} not accepted (HTTP {rr.status_code})")
    print(f"speculative_turn applied: {spec_applied}")
    # 2d. calm, non-expressive delivery (Realtime may ignore stored TTS; Custom mode uses these)
    tts = cc.get("tts") or {}
    tts_patch = {"expressive_mode": TTS_EXPRESSIVE_MODE, "stability": TTS_STABILITY}
    print(f"tts.expressive_mode: {tts.get('expressive_mode')} -> {TTS_EXPRESSIVE_MODE}; "
          f"tts.stability: {tts.get('stability')} -> {TTS_STABILITY} (voice_id / model_id unchanged)")
    rr = c.patch(f"{API}/agents/{aid}", json={"conversation_config": {"tts": tts_patch}})
    if rr.status_code >= 400:
        print(f"tts calm settings not accepted (HTTP {rr.status_code}): {rr.text[:300]}")
        tts_applied = False
    else:
        got = ((rr.json().get("conversation_config") or {}).get("tts") or {})
        tts_applied = got.get("expressive_mode") is TTS_EXPRESSIVE_MODE and abs(float(got.get("stability") or 0) - TTS_STABILITY) < 1e-6
        print(f"tts calm settings applied: expressive_mode={got.get('expressive_mode')} stability={got.get('stability')}")
    # 3. read back and verify
    after = c.get(f"{API}/agents/{aid}").json()
    acc = after["conversation_config"]
    ok = {"language": acc["agent"].get("language") == LANGUAGE,
          "first_message": acc["agent"].get("first_message") == FIRST_MESSAGE,
          "prompt": acc["agent"]["prompt"].get("prompt") == SYSTEM_PROMPT,
          "tool_ids": set(ids) <= set(acc["agent"]["prompt"].get("tool_ids") or []),
          "client_tool_call": "client_tool_call" in (acc["conversation"].get("client_events") or []),
          "enable_auth": (after.get("platform_settings") or {}).get("auth", {}).get("enable_auth") is True,
          "voice_unchanged": acc["tts"].get("voice_id") == cc["tts"].get("voice_id"),
          "tts_model_kept": acc["tts"].get("model_id") == cc["tts"].get("model_id"),
          "llm_kept": acc["agent"]["prompt"].get("llm") == p.get("llm"),
          "realtime_model": acc.get("realtime_model") == want_rt,
          "turn_eagerness": acc["turn"].get("turn_eagerness") == TURN_EAGERNESS,
          "vad_score_event": "vad_score" in (acc["conversation"].get("client_events") or []),
          "tentative_transcript_event": "tentative_user_transcript" in (acc["conversation"].get("client_events") or []),
          "interruption_event": "interruption" in (acc["conversation"].get("client_events") or []),
          "max_tokens_ok": want_rt != "eleven_realtime_v1_max" or acc["agent"]["prompt"].get("max_tokens") in (-1, None)
                           or acc["agent"]["prompt"].get("max_tokens") >= REALTIME_MIN_MAX_TOKENS,
          "asr_kept": acc.get("asr") == cc.get("asr"),
          "turn_timeout": turn_applied is not None and acc["turn"].get("turn_timeout") == turn_applied,
          "soft_timeout_off": (acc["turn"].get("soft_timeout_config") or {}).get("timeout_seconds") == SOFT_TIMEOUT_S,
          "no_silence_hangup": acc["turn"].get("silence_end_call_timeout") in (-1, -1.0, None),
          "speculative_turn": acc["turn"].get("speculative_turn") is SPECULATIVE_TURN,
          "tts_expressive_off": acc["tts"].get("expressive_mode") is TTS_EXPRESSIVE_MODE,
          "tts_stability": abs(float(acc["tts"].get("stability") or 0) - TTS_STABILITY) < 1e-6}
    print("\nread-back check:", ok)
    (ROOT / "secrets" / f"elevenlabs_agent_after_{ts}.json").write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"voice mode: {'Realtime ' + str(acc.get('realtime_model')) if acc.get('realtime_model') else 'Custom'}"
          f"{'' if not rt_error else ' (Realtime error: ' + rt_error + ')'}; revert: --custom")
    if not all(ok.values()):
        sys.exit("some fields did not take effect, see above")


if __name__ == "__main__":
    main()
