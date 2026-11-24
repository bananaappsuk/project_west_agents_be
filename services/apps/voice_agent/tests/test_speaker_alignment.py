"""Who said what: joining whisper's words to the speaker model's turns.

The thing that must never happen is a word attributed to the wrong person, or a
word lost. These pin the joining logic and the ways it is allowed to give up --
it gives up by returning the plain transcript, never by failing the call.
"""

from __future__ import annotations

import pytest

from app import transcribe as t
from app.config import settings


def w(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end}


TURNS = [
    {"speaker": "A", "start": 0.0, "end": 4.5},   # the recorded notice
    {"speaker": "B", "start": 4.8, "end": 7.0},   # staff
    {"speaker": "C", "start": 7.2, "end": 12.0},  # family
]


class TestSpeakerAt:
    def test_inside_a_turn(self):
        assert t.speaker_at(5.5, TURNS) == "B"

    def test_in_a_gap_takes_the_nearest(self):
        """The two models never agree exactly on boundaries."""
        assert t.speaker_at(4.6, TURNS) == "A"
        assert t.speaker_at(7.15, TURNS) == "C"

    def test_no_turns(self):
        assert t.speaker_at(1.0, []) is None


class TestAlign:
    def test_a_segment_spanning_two_speakers_is_split_with_its_punctuation(self):
        """Whisper often runs a greeting and the reply into one segment. Split by
        word, not by segment, or the caller's first words go to staff."""
        result = {
            "segments": [{"start": 4.8, "end": 9.0, "text": " Hello, Project West. Good morning."}],
            "words": [
                w("Hello", 4.9, 5.2), w("Project", 5.3, 5.7), w("West", 5.8, 6.2),
                w("Good", 7.4, 7.7), w("morning", 7.8, 8.4),
            ],
        }
        out = t.align(result, TURNS)
        assert [(s["speaker"], s["text"]) for s in out] == [
            ("B", "Hello, Project West."),
            ("C", "Good morning."),
        ]

    def test_consecutive_lines_from_one_speaker_become_one_turn(self):
        result = {
            "segments": [
                {"start": 7.3, "end": 8.0, "text": " I'm calling"},
                {"start": 8.1, "end": 9.5, "text": " about Sunday."},
            ],
            "words": [w("I'm", 7.3, 7.6), w("calling", 7.6, 8.0),
                      w("about", 8.1, 8.5), w("Sunday", 8.6, 9.5)],
        }
        out = t.align(result, TURNS)
        assert len(out) == 1
        assert out[0]["speaker"] == "C"
        assert out[0]["text"] == "I'm calling about Sunday."

    def test_a_count_mismatch_keeps_every_word_and_votes(self):
        """Where tokens and words disagree, nothing is dropped or reordered: the
        whole segment goes to the speaker most of it belonged to."""
        result = {
            "segments": [{"start": 7.3, "end": 9.0, "text": " The 10-12 slot, please."}],
            # whisper split "10-12" into two words, so the counts differ
            "words": [w("The", 7.3, 7.5), w("10", 7.6, 7.8), w("12", 7.9, 8.1),
                      w("slot", 8.2, 8.5), w("please", 8.6, 9.0)],
        }
        out = t.align(result, TURNS)
        assert out == [{"speaker": "C", "start": 7.3, "end": 9.0,
                        "text": "The 10-12 slot, please."}]

    def test_no_words_lost_across_a_whole_call(self):
        result = {
            "segments": [
                {"start": 0.0, "end": 4.5, "text": " This call is being recorded."},
                {"start": 4.8, "end": 6.5, "text": " Good morning."},
                {"start": 7.3, "end": 11.0, "text": " Hi, it's about my children."},
            ],
            "words": [
                w("This", 0.0, 0.4), w("call", 0.5, 0.9), w("is", 1.0, 1.1),
                w("being", 1.2, 1.6), w("recorded", 1.7, 4.4),
                w("Good", 4.9, 5.3), w("morning", 5.4, 6.4),
                w("Hi", 7.3, 7.6), w("it's", 7.7, 8.0), w("about", 8.1, 8.6),
                w("my", 8.7, 9.0), w("children", 9.1, 10.9),
            ],
        }
        out = t.align(result, TURNS)
        rebuilt = " ".join(s["text"] for s in out)
        original = " ".join(seg["text"].strip() for seg in result["segments"])
        assert rebuilt == original
        assert [s["speaker"] for s in out] == ["A", "B", "C"]


class TestDegradesRatherThanFails:
    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        monkeypatch.setattr(settings, "openai_api_key", "test-key")
        monkeypatch.setattr(settings, "speaker_labels_enabled", True)

    WORDS = {
        "text": "Good morning. Hi.",
        "segments": [{"start": 4.8, "end": 6.5, "text": " Good morning."},
                     {"start": 7.3, "end": 8.0, "text": " Hi."}],
        "words": [w("Good", 4.9, 5.3), w("morning", 5.4, 6.4), w("Hi", 7.3, 8.0)],
    }

    def test_a_failed_speaker_pass_still_returns_the_transcript(self, monkeypatch):
        monkeypatch.setattr(t, "_words_pass", lambda a, c: self.WORDS)

        def boom(a, c):
            raise RuntimeError("speaker model down")

        monkeypatch.setattr(t, "_speakers_pass", boom)
        text, segments = t.transcribe_detailed(b"audio", "audio/mpeg")
        assert text == "Good morning. Hi."
        assert segments is None

    def test_a_failed_label_step_leaves_neutral_labels(self, monkeypatch):
        monkeypatch.setattr(t, "_words_pass", lambda a, c: self.WORDS)
        monkeypatch.setattr(t, "_speakers_pass", lambda a, c: TURNS)

        def boom(turns):
            raise RuntimeError("label model down")

        monkeypatch.setattr(t, "label_speakers", boom)
        text, segments = t.transcribe_detailed(b"audio", "audio/mpeg")
        assert text == "Good morning. Hi."
        assert [s["speaker"] for s in segments] == ["B", "C"]
        assert all(s["role"] is None for s in segments)

    def test_labels_are_applied_when_confident(self, monkeypatch):
        monkeypatch.setattr(t, "_words_pass", lambda a, c: self.WORDS)
        monkeypatch.setattr(t, "_speakers_pass", lambda a, c: TURNS)
        monkeypatch.setattr(t, "label_speakers", lambda turns: {"B": "staff", "C": "family"})
        _, segments = t.transcribe_detailed(b"audio", "audio/mpeg")
        assert [s["role"] for s in segments] == ["staff", "family"]

    def test_switched_off_uses_the_plain_transcript_only(self, monkeypatch):
        monkeypatch.setattr(settings, "speaker_labels_enabled", False)
        monkeypatch.setattr(t, "transcribe", lambda a, c: "plain text")

        def must_not_run(a, c):
            raise AssertionError("speaker pass ran while switched off")

        monkeypatch.setattr(t, "_speakers_pass", must_not_run)
        assert t.transcribe_detailed(b"audio", "audio/mpeg") == ("plain text", None)

    def test_no_key_returns_empty_without_calling_out(self, monkeypatch):
        monkeypatch.setattr(settings, "openai_api_key", None)
        assert t.transcribe_detailed(b"audio", "audio/mpeg") == ("", None)


class TestLabelConfidence:
    def test_low_confidence_labels_nobody(self, monkeypatch):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content":
                    '{"mapping": {"A": "notice", "B": "staff"}, "confidence": "low"}'}}]}

        class C:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, *a, **k): return R()

        monkeypatch.setattr(t.httpx, "Client", C)
        assert t.label_speakers([{"speaker": "A", "text": "x"}]) == {}

    def test_only_known_roles_survive(self, monkeypatch):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content":
                    '{"mapping": {"A": "notice", "B": "staff", "C": "unknown", "D": "manager"},'
                    ' "confidence": "high"}'}}]}

        class C:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, *a, **k): return R()

        monkeypatch.setattr(t.httpx, "Client", C)
        assert t.label_speakers([{"speaker": "A", "text": "x"}]) == {"A": "notice", "B": "staff"}
