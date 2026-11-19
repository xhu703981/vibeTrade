"""ElevenLabs: the backend trades the API key for a session signed URL (the key never leaves the backend or enters logs)."""
from __future__ import annotations

import httpx

from backend.config import settings

API = "https://api.elevenlabs.io"


class VoiceConfigError(RuntimeError):
    pass


async def get_signed_url(client: httpx.AsyncClient | None = None) -> str:
    if not settings.elevenlabs_api_key or not settings.elevenlabs_agent_id:
        raise VoiceConfigError("ELEVENLABS_API_KEY / ELEVENLABS_AGENT_ID is not set")
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        r = await client.get(f"{API}/v1/convai/conversation/get-signed-url",
                             params={"agent_id": settings.elevenlabs_agent_id},
                             headers={"xi-api-key": settings.elevenlabs_api_key})
    finally:
        if own:
            await client.aclose()
    if r.status_code != 200:
        # never pass the response body back (it may hold account info), only the status code
        raise VoiceConfigError(f"ElevenLabs returned HTTP {r.status_code}")
    url = (r.json() or {}).get("signed_url")
    if not isinstance(url, str) or not url.startswith("wss://"):
        raise VoiceConfigError("ElevenLabs returned no signed_url")
    return url
