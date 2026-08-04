"""Audio transcription — shared by every recording source (BT Cloud, S3-compatible bucket, …).

Provider-agnostic: takes raw audio bytes + content type, returns text via OpenAI's
transcription API. Extracted out of bt_client.py so a second source doesn't duplicate it.
"""

from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("voice_agent.transcribe")


def transcribe(audio: bytes, content_type: str) -> str:
    if not settings.openai_api_key:
        log.warning("OPENAI_API_KEY unset — storing recording with an empty transcript")
        return ""
    ext = "wav" if "wav" in content_type else "mp3"
    with httpx.Client(timeout=180.0) as c:
        r = c.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={"file": (f"recording.{ext}", audio, content_type or "audio/mpeg")},
            data={"model": settings.transcribe_model},
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
