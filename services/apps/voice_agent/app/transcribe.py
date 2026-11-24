"""Audio transcription — shared by every recording source (BT Cloud, S3-compatible bucket, …).

Provider-agnostic: takes raw audio bytes + content type, returns text via OpenAI's
transcription API. Extracted out of bt_client.py so a second source doesn't duplicate it.

Who said what
-------------
`transcribe_detailed` also returns the transcript split by speaker. It takes two
passes and joins them:

  * the WORDS come from `transcribe_model` (whisper-1), with word timings;
  * WHO SPOKE WHEN comes from `diarize_model`, which hears voices apart but hears
    words worse.

Each word is given to whichever speaker was talking at that moment, and runs of
words from the same speaker become one turn. Tested on real calls before this was
written: the speaker model on its own dropped a caller's name entirely and turned a
child's first name into "80", while whisper-1 heard both correctly. On a contact
centre's records the words are the part that cannot be allowed to get worse.

Recordings are mono -- caller and staff on one channel -- so speakers are told
apart by voice, not by channel, and labels are an aid to reading rather than a
statement of who said each word. The plain `transcript` stays the record.

Everything here degrades rather than fails. No speaker pass, a failed speaker pass,
or a failed labelling step all still return the plain transcript; only the
segments are missing.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("voice_agent.transcribe")

_API = "https://api.openai.com/v1"

# Roles the labelling step may assign. Anything else -- including the model's own
# "unknown" -- is shown as a neutral "Speaker X".
ROLES = {"notice", "staff", "family"}


def _ext(content_type: str) -> str:
    return "wav" if "wav" in (content_type or "") else "mp3"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.openai_api_key}"}


def transcribe(audio: bytes, content_type: str) -> str:
    if not settings.openai_api_key:
        log.warning("OPENAI_API_KEY unset — storing recording with an empty transcript")
        return ""
    with httpx.Client(timeout=180.0) as c:
        r = c.post(
            f"{_API}/audio/transcriptions",
            headers=_headers(),
            files={"file": (f"recording.{_ext(content_type)}", audio, content_type or "audio/mpeg")},
            data={"model": settings.transcribe_model},
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()


# ---- the two passes -------------------------------------------------------


def _words_pass(audio: bytes, content_type: str) -> dict[str, Any]:
    """The accurate words, with timings for each segment and each word."""
    with httpx.Client(timeout=180.0) as c:
        r = c.post(
            f"{_API}/audio/transcriptions",
            headers=_headers(),
            files={"file": (f"recording.{_ext(content_type)}", audio, content_type or "audio/mpeg")},
            data={
                "model": settings.transcribe_model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["segment", "word"],
            },
        )
        r.raise_for_status()
        return r.json()


def _speakers_pass(audio: bytes, content_type: str) -> list[dict[str, Any]]:
    """Who spoke when. Its own words are discarded -- only speaker and timing kept.

    `chunking_strategy` is required for anything over 30 seconds, which is every
    call this centre takes.
    """
    with httpx.Client(timeout=300.0) as c:
        r = c.post(
            f"{_API}/audio/transcriptions",
            headers=_headers(),
            files={"file": (f"recording.{_ext(content_type)}", audio, content_type or "audio/mpeg")},
            data={
                "model": settings.diarize_model,
                "response_format": "diarized_json",
                "chunking_strategy": "auto",
            },
        )
        r.raise_for_status()
        return [
            {"speaker": str(s.get("speaker")), "start": float(s["start"]), "end": float(s["end"])}
            for s in (r.json().get("segments") or [])
            if s.get("start") is not None and s.get("end") is not None
        ]


# ---- joining them --------------------------------------------------------


def speaker_at(t: float, turns: list[dict[str, Any]]) -> str | None:
    """Who was speaking at time `t`. Inside a turn wins; otherwise the nearest one,
    since the two models' timings never line up exactly at the boundaries."""
    if not turns:
        return None
    best, best_gap = None, float("inf")
    for turn in turns:
        if turn["start"] <= t <= turn["end"]:
            return turn["speaker"]
        gap = min(abs(t - turn["start"]), abs(t - turn["end"]))
        if gap < best_gap:
            best, best_gap = turn["speaker"], gap
    return best


def align(words_result: dict[str, Any], turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each word to a speaker, then merge runs into turns.

    Punctuation lives in whisper's segment text, not its word list, so words are
    matched back to the segment's whitespace-split tokens in order. Where the
    counts disagree -- rare, and usually a stray hyphen or number -- the whole
    segment goes to whichever speaker most of its words belonged to, so text is
    never dropped or reordered.
    """
    words = words_result.get("words") or []
    segments = words_result.get("segments") or []
    pieces: list[dict[str, Any]] = []

    wi = 0
    for seg in segments:
        s_start, s_end = float(seg.get("start", 0)), float(seg.get("end", 0))
        tokens = (seg.get("text") or "").split()
        seg_words = []
        while wi < len(words) and float(words[wi].get("start", 0)) < s_end + 0.01:
            seg_words.append(words[wi])
            wi += 1

        if tokens and len(tokens) == len(seg_words):
            for tok, w in zip(tokens, seg_words):
                mid = (float(w["start"]) + float(w["end"])) / 2
                pieces.append({"speaker": speaker_at(mid, turns), "start": float(w["start"]),
                               "end": float(w["end"]), "text": tok})
        elif tokens:
            votes: dict[str | None, int] = {}
            for w in seg_words or [{"start": s_start, "end": s_end}]:
                who = speaker_at((float(w["start"]) + float(w["end"])) / 2, turns)
                votes[who] = votes.get(who, 0) + 1
            who = max(votes, key=votes.get)
            pieces.append({"speaker": who, "start": s_start, "end": s_end, "text": " ".join(tokens)})

    merged: list[dict[str, Any]] = []
    for p in pieces:
        if merged and merged[-1]["speaker"] == p["speaker"]:
            merged[-1]["text"] += " " + p["text"]
            merged[-1]["end"] = p["end"]
        else:
            merged.append(dict(p))
    for m in merged:
        m["start"] = round(m["start"], 2)
        m["end"] = round(m["end"], 2)
    return merged


# ---- naming the speakers -------------------------------------------------


def label_speakers(turns: list[dict[str, Any]]) -> dict[str, str]:
    """Which letter is the recorded notice, which is Project West, which is the family.

    Read from what was said, not from order: the recorded notice always plays
    first, so "first voice = staff" would be wrong on every call, and calls go
    both ways -- staff ring families too. Returns only confident labels; a
    speaker it cannot place is simply left out and shown neutrally.
    """
    sample = "\n".join(f"{t['speaker']}: {t['text']}" for t in turns[:18])
    prompt = (
        "These are the opening lines of a phone call involving Project West, a UK "
        "child-contact centre. Calls can be inbound or outbound. An automated notice "
        "that the call is being recorded usually plays first. A Project West staff "
        "member introduces the centre or themselves (the name is often misheard, e.g. "
        "'Project First', 'Project Quest'). The other party is a parent, family "
        "member or professional.\n\n"
        "Map each speaker label to one of: notice, staff, family, unknown. Use "
        "unknown unless you are confident.\n"
        'Reply as JSON: {"mapping": {"<label>": "<role>"}, "confidence": "high|medium|low"}\n\n'
        + sample
    )
    with httpx.Client(timeout=60.0) as c:
        r = c.post(
            f"{_API}/chat/completions",
            headers=_headers(),
            json={
                "model": settings.speaker_label_model,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        r.raise_for_status()
        out = json.loads(r.json()["choices"][0]["message"]["content"])
    if str(out.get("confidence", "")).lower() == "low":
        return {}
    mapping = out.get("mapping") or {}
    return {str(k): v for k, v in mapping.items() if v in ROLES}


# ---- the entry point -----------------------------------------------------


def transcribe_detailed(audio: bytes, content_type: str) -> tuple[str, list[dict[str, Any]] | None]:
    """The transcript, plus the same call split by speaker where that worked.

    Returns (text, segments). `text` is what `transcribe` would have returned, and
    is always present when transcription itself succeeded. `segments` is None
    whenever speaker labelling is switched off or any part of it fails.
    """
    if not settings.openai_api_key:
        log.warning("OPENAI_API_KEY unset — storing recording with an empty transcript")
        return "", None
    if not settings.speaker_labels_enabled:
        return transcribe(audio, content_type), None

    # Run side by side: the call costs the slower of the two, not the sum.
    with ThreadPoolExecutor(max_workers=2) as pool:
        words_f = pool.submit(_words_pass, audio, content_type)
        turns_f = pool.submit(_speakers_pass, audio, content_type)
        words_result = words_f.result()  # a failure here is a real transcription failure
        try:
            turns = turns_f.result()
        except Exception as exc:
            log.warning("speaker pass failed; keeping the plain transcript: %s", exc)
            turns = []

    text = (words_result.get("text") or "").strip()
    if not turns or not text:
        return text, None

    try:
        segments = align(words_result, turns)
    except Exception as exc:
        log.warning("could not align speakers to words; keeping the plain transcript: %s", exc)
        return text, None
    if not segments:
        return text, None

    try:
        roles = label_speakers(segments)
    except Exception as exc:
        log.warning("speaker labelling failed; showing neutral labels: %s", exc)
        roles = {}
    for seg in segments:
        seg["role"] = roles.get(seg["speaker"])
    return text, segments
