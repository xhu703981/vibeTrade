"""What does the live agent (Eleven Realtime) emit while the user is still speaking?

Synthesises one spoken sentence with ElevenLabs TTS (pcm_16000), streams it into a real agent session at real-time pace
(100 ms chunks, then silence), and logs every server event with its time relative to the start of speech:
tentative_user_transcript (partials), user_transcript (final), vad_score, agent_response, client_tool_call.
Tool calls are answered with ok=false (nothing reaches the backend). Uses a little ElevenLabs credit (TTS + ~1 minute).
Needs the backend running (signed URL). Never prints the API key.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

from backend.config import settings

ROOT = Path(__file__).resolve().parent.parent
VOICE = "21m00Tcm4TlvDq8ikWAM"   # a premade voice, only used to synthesise the test speech


async def tts(text: str) -> bytes:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}", params={"output_format": "pcm_16000"},
                         headers={"xi-api-key": settings.elevenlabs_api_key}, json={"text": text, "model_id": "eleven_flash_v2_5"})
        r.raise_for_status()
        return r.content


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--text", default="Okay so I want to put in fifty dollars total, and at most ten dollars per trade.")
    a = ap.parse_args()
    pcm = await tts(a.text)
    async with httpx.AsyncClient(base_url=a.base, timeout=30) as c:
        signed = (await c.get("/voice/signed-url")).json()["signed_url"]
    log: list[dict] = []
    t0 = None
    ws = await websockets.connect(signed, max_size=16 * 2**20)
    await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
    fmt = None
    agent_quiet = asyncio.Event()
    last_audio = time.time()

    async def reader():
        nonlocal fmt, last_audio
        async for raw in ws:
            m = json.loads(raw)
            t = m.get("type")
            rel = None if t0 is None else round(time.time() - t0, 3)
            if t == "ping":
                await ws.send(json.dumps({"type": "pong", "event_id": m["ping_event"]["event_id"]}))
                continue
            if t == "audio":
                last_audio = time.time()
                if t0 is not None:
                    log.append({"t": rel, "type": "audio"})
                continue
            if t == "conversation_initiation_metadata":
                fmt = m["conversation_initiation_metadata_event"].get("user_input_audio_format")
            if t == "client_tool_call":
                cc = m["client_tool_call"]
                await ws.send(json.dumps({"type": "client_tool_result", "tool_call_id": cc["tool_call_id"],
                                          "result": json.dumps({"ok": False, "reason": "probe only, nothing was changed"}), "is_error": False}))
            entry = {"t": rel, "type": t}
            if t == "tentative_user_transcript":
                entry["text"] = m["tentative_user_transcription_event"]["user_transcript"]
            elif t == "user_transcript":
                entry["text"] = m["user_transcription_event"]["user_transcript"]
            elif t == "vad_score":
                entry["score"] = round(m["vad_score_event"]["vad_score"], 2)
            elif t == "agent_response":
                entry["text"] = m["agent_response_event"]["agent_response"][:120]
            elif t == "client_tool_call":
                entry["text"] = m["client_tool_call"]["tool_name"]
            else:
                entry["keys"] = [k for k in m if k != "type"]
            log.append(entry)

    rt = asyncio.create_task(reader())
    # let the first message play out
    await asyncio.sleep(3)
    while time.time() - last_audio < 2.5:
        await asyncio.sleep(0.3)
    print("user_input_audio_format:", fmt)
    chunk = 3200   # 100 ms of 16 kHz 16-bit mono
    t0 = time.time()
    for i in range(0, len(pcm), chunk):
        await ws.send(json.dumps({"user_audio_chunk": base64.b64encode(pcm[i:i + chunk]).decode()}))
        await asyncio.sleep(0.1)
    speech_end = round(time.time() - t0, 3)
    for _ in range(60):   # 6 s of silence
        await ws.send(json.dumps({"user_audio_chunk": base64.b64encode(bytes(chunk)).decode()}))
        await asyncio.sleep(0.1)
    await asyncio.sleep(4)
    rt.cancel()
    await ws.close()
    out = {"text": a.text, "speech_s": speech_end, "format": fmt, "events": log}
    p = ROOT / "runs" / f"partials_probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"speech 0.000 -> {speech_end}s")
    vads = [e for e in log if e["type"] == "vad_score"]
    print(f"vad_score events: {len(vads)}")
    for e in log:
        if e["type"] not in ("audio", "vad_score"):
            print(f"  {str(e['t']):>7}  {e['type']:<28} {e.get('text', e.get('keys', ''))}")
    aud = [e["t"] for e in log if e["type"] == "audio"]
    print(f"first agent audio after speech start: {aud[0] if aud else None}s")
    print(f"saved {p}")


if __name__ == "__main__":
    asyncio.run(main())
