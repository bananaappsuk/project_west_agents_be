"""Transcription: the call, and every way the speaker pass is allowed to fail.

AC-10's "voice processing" coverage. `test_speaker_alignment.py` already covers
`align`, `speaker_at` and `label_speakers` -- the arithmetic. What had no tests
is the orchestration around them: whether a missing API key stops the call,
whether a failed speaker pass costs the transcript, and whether a failure in
alignment or labelling degrades or propagates.

That matters because the degradation is the designed behaviour: a call whose
speakers could not be separated must still keep its transcript. A regression
there would show as silently empty transcripts.

No network: `httpx.Client` and the two passes are doubled. Audio is a handful
of synthetic bytes.

    .venv/Scripts/python.exe -m pytest tests/test_transcription.py -q
"""

from __future__ import annotations

import pytest

from app import transcribe

AUDIO = b"SYNTHETIC-AUDIO-NOT-A-REAL-CALL"
TEXT = "Synthetic transcript. No real call."

WORDS = {
    "text": TEXT,
    "words": [
        {"word": "Synthetic", "start": 0.0, "end": 0.5},
        {"word": "transcript.", "start": 0.6, "end": 1.2},
    ],
}
TURNS = [
    {"speaker": "A", "start": 0.0, "end": 1.0},
    {"speaker": "B", "start": 1.0, "end": 2.0},
]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """A key present and speaker labels on, unless a test says otherwise."""
    monkeypatch.setattr(transcribe.settings, "openai_api_key", "sk-synthetic", raising=False)
    monkeypatch.setattr(transcribe.settings, "speaker_labels_enabled", True, raising=False)
    monkeypatch.setattr(transcribe.settings, "transcribe_model", "whisper-1", raising=False)


class _Response:
    def __init__(self, payload, *, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        return self._payload


class _Client:
    """Stands in for httpx.Client, recording the one post it receives."""

    posted: list[dict] = []

    def __init__(self, *_a, **kw):
        self.kw = kw
        self.response = _Response({"text": "  %s  " % TEXT})

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def post(self, url, *, headers=None, files=None, data=None):
        type(self).posted.append(
            {"url": url, "headers": headers, "files": files, "data": data}
        )
        return self.response


@pytest.fixture
def client(monkeypatch):
    _Client.posted = []
    monkeypatch.setattr(transcribe.httpx, "Client", _Client)
    return _Client


# ── the content-type to file-extension mapping ──────────────────────────────
@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("audio/wav", "wav"),
        ("audio/x-wav", "wav"),
        ("audio/mpeg", "mp3"),
        ("audio/mp3", "mp3"),
        ("", "mp3"),
        (None, "mp3"),
        ("application/octet-stream", "mp3"),
    ],
)
def test_the_upload_filename_extension_follows_the_content_type(content_type, expected):
    assert transcribe._ext(content_type) == expected


# ── transcribe ──────────────────────────────────────────────────────────────
def test_no_api_key_yields_an_empty_transcript_without_calling_out(monkeypatch, client):
    """Deliberate: a recording is stored with no transcript rather than lost."""
    monkeypatch.setattr(transcribe.settings, "openai_api_key", "", raising=False)
    assert transcribe.transcribe(AUDIO, "audio/mpeg") == ""
    assert client.posted == [], "no request may be made without a key"


def test_a_transcript_is_returned_stripped(client):
    assert transcribe.transcribe(AUDIO, "audio/mpeg") == TEXT


def test_the_request_names_the_model_and_the_audio(client):
    transcribe.transcribe(AUDIO, "audio/wav")
    sent = client.posted[0]
    assert sent["url"].endswith("/audio/transcriptions")
    assert sent["data"] == {"model": "whisper-1"}
    filename, body, content_type = sent["files"]["file"]
    assert filename == "recording.wav"
    assert body == AUDIO
    assert content_type == "audio/wav"
    assert sent["headers"]["Authorization"] == "Bearer sk-synthetic"


def test_a_missing_content_type_still_sends_something_valid(client):
    transcribe.transcribe(AUDIO, "")
    filename, _, content_type = client.posted[0]["files"]["file"]
    assert filename == "recording.mp3"
    assert content_type == "audio/mpeg"


def test_an_http_error_propagates(monkeypatch, client):
    """A transcription failure must not be mistaken for an empty call."""
    class _Failing(_Client):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.response = _Response({}, status=500)

    monkeypatch.setattr(transcribe.httpx, "Client", _Failing)
    with pytest.raises(RuntimeError):
        transcribe.transcribe(AUDIO, "audio/mpeg")


def test_an_empty_response_body_yields_an_empty_string(monkeypatch, client):
    class _Blank(_Client):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.response = _Response({})

    monkeypatch.setattr(transcribe.httpx, "Client", _Blank)
    assert transcribe.transcribe(AUDIO, "audio/mpeg") == ""


# ── transcribe_detailed ─────────────────────────────────────────────────────
def test_detailed_needs_a_key_too(monkeypatch, client):
    monkeypatch.setattr(transcribe.settings, "openai_api_key", "", raising=False)
    assert transcribe.transcribe_detailed(AUDIO, "audio/mpeg") == ("", None)
    assert client.posted == []


def test_speaker_labels_switched_off_falls_back_to_one_pass(monkeypatch, client):
    monkeypatch.setattr(transcribe.settings, "speaker_labels_enabled", False, raising=False)
    text, segments = transcribe.transcribe_detailed(AUDIO, "audio/mpeg")
    assert text == TEXT
    assert segments is None
    assert len(client.posted) == 1, "one pass, not two"


def test_both_passes_succeeding_yields_segments_with_roles(monkeypatch):
    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: dict(WORDS))
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: list(TURNS))
    monkeypatch.setattr(transcribe, "align", lambda *_a: [
        {"speaker": "A", "text": "Synthetic", "start": 0.0, "end": 0.5},
        {"speaker": "B", "text": "transcript.", "start": 0.6, "end": 1.2},
    ])
    monkeypatch.setattr(transcribe, "label_speakers", lambda _s: {"A": "staff", "B": "family"})

    text, segments = transcribe.transcribe_detailed(AUDIO, "audio/mpeg")
    assert text == TEXT
    assert [s["role"] for s in segments] == ["staff", "family"]


def test_a_failed_speaker_pass_keeps_the_transcript(monkeypatch):
    """The designed degradation: separation is a bonus, the transcript is not."""
    def boom(*_a):
        raise RuntimeError("speaker pass unavailable")

    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: dict(WORDS))
    monkeypatch.setattr(transcribe, "_speakers_pass", boom)

    text, segments = transcribe.transcribe_detailed(AUDIO, "audio/mpeg")
    assert text == TEXT
    assert segments is None


def test_a_failed_words_pass_is_a_real_failure(monkeypatch):
    """Not degraded: without words there is no transcript to keep."""
    def boom(*_a):
        raise RuntimeError("transcription unavailable")

    monkeypatch.setattr(transcribe, "_words_pass", boom)
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: list(TURNS))

    with pytest.raises(RuntimeError):
        transcribe.transcribe_detailed(AUDIO, "audio/mpeg")


def test_no_turns_keeps_the_transcript_without_segments(monkeypatch):
    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: dict(WORDS))
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: [])
    text, segments = transcribe.transcribe_detailed(AUDIO, "audio/mpeg")
    assert (text, segments) == (TEXT, None)


def test_empty_text_yields_no_segments_even_with_turns(monkeypatch):
    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: {"text": "   ", "words": []})
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: list(TURNS))
    assert transcribe.transcribe_detailed(AUDIO, "audio/mpeg") == ("", None)


def test_a_failed_alignment_keeps_the_transcript(monkeypatch):
    def boom(*_a):
        raise ValueError("cannot align")

    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: dict(WORDS))
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: list(TURNS))
    monkeypatch.setattr(transcribe, "align", boom)

    assert transcribe.transcribe_detailed(AUDIO, "audio/mpeg") == (TEXT, None)


def test_an_empty_alignment_keeps_the_transcript(monkeypatch):
    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: dict(WORDS))
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: list(TURNS))
    monkeypatch.setattr(transcribe, "align", lambda *_a: [])
    assert transcribe.transcribe_detailed(AUDIO, "audio/mpeg") == (TEXT, None)


def test_failed_labelling_keeps_the_segments_with_neutral_roles(monkeypatch):
    """Labelling is the most speculative step; losing it must not lose the split."""
    def boom(_s):
        raise RuntimeError("labelling failed")

    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: dict(WORDS))
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: list(TURNS))
    monkeypatch.setattr(transcribe, "align", lambda *_a: [
        {"speaker": "A", "text": "Synthetic", "start": 0.0, "end": 0.5},
    ])
    monkeypatch.setattr(transcribe, "label_speakers", boom)

    text, segments = transcribe.transcribe_detailed(AUDIO, "audio/mpeg")
    assert text == TEXT
    assert segments and segments[0]["role"] is None


def test_an_unlabelled_speaker_gets_a_null_role(monkeypatch):
    monkeypatch.setattr(transcribe, "_words_pass", lambda *_a: dict(WORDS))
    monkeypatch.setattr(transcribe, "_speakers_pass", lambda *_a: list(TURNS))
    monkeypatch.setattr(transcribe, "align", lambda *_a: [
        {"speaker": "A", "text": "one", "start": 0.0, "end": 0.5},
        {"speaker": "Z", "text": "two", "start": 0.6, "end": 1.0},
    ])
    monkeypatch.setattr(transcribe, "label_speakers", lambda _s: {"A": "staff"})

    _, segments = transcribe.transcribe_detailed(AUDIO, "audio/mpeg")
    assert [s["role"] for s in segments] == ["staff", None]


def test_the_known_roles_are_the_three_the_brief_names():
    assert transcribe.ROLES == {"notice", "staff", "family"}
