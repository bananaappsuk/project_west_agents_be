"""The two recording sources: what they list, and the metadata they produce.

AC-10's "integration" coverage for ingestion. `test_source_config.py` already
covers which source is *chosen* and the refusal of a half-configured
deployment. What had no tests is what each client then does: the endpoint
normalisation both providers need, the metadata shape `pipeline._run` depends
on, and BT's token refresh.

The metadata shape matters more than it looks. `pipeline._run` reads six keys
off every listed item and `Recording` is built from them directly, so a
provider changing a field name would surface as blank callers and wrong dates
rather than as an error.

No network and no cloud SDK calls: boto3's client and httpx are doubled.
Everything is synthetic.

    .venv/Scripts/python.exe -m pytest tests/test_ingestion_sources.py -q
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest

from app import bt_client, s3_client

# ── S3: endpoint normalisation ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "given,expected",
    [
        ("s3.us-east-005.backblazeb2.com", "https://s3.us-east-005.backblazeb2.com"),
        ("https://s3.example.test", "https://s3.example.test"),
        ("http://localhost:9000", "http://localhost:9000"),
        ("", None),
        (None, None),
    ],
)
def test_a_bare_endpoint_gains_a_scheme(given, expected):
    """Providers display endpoints without a scheme; boto3 requires a full URL."""
    assert s3_client._normalize_endpoint(given) == expected


# ── S3: listing ─────────────────────────────────────────────────────────────
class _Paginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[dict] = []

    def paginate(self, **kw):
        self.calls.append(kw)
        return self.pages


class _S3:
    """Stands in for a boto3 S3 client."""

    def __init__(self, pages, metadata=None, *, head_raises=False):
        self.paginator = _Paginator(pages)
        self.metadata = metadata or {}
        self.head_raises = head_raises
        self.heads: list[str] = []

    def get_paginator(self, _name):
        return self.paginator

    def head_object(self, *, Bucket, Key):  # noqa: N803 -- boto3's own kwarg names
        self.heads.append(Key)
        if self.head_raises:
            raise RuntimeError("head_object denied")
        return {"Metadata": self.metadata.get(Key, {})}


def _obj(key, *, when):
    return {"Key": key, "LastModified": when}


T1 = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
T3 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


def test_objects_come_back_newest_first():
    client = _S3([{"Contents": [_obj("a.mp3", when=T1), _obj("c.mp3", when=T3),
                                _obj("b.mp3", when=T2)]}])
    out = s3_client._list_objects(client, bucket="b", prefix="", count=10)
    assert [o["Key"] for o in out] == ["c.mp3", "b.mp3", "a.mp3"]


def test_folder_placeholders_are_not_recordings():
    client = _S3([{"Contents": [_obj("calls/", when=T3), _obj("calls/a.mp3", when=T2)]}])
    out = s3_client._list_objects(client, bucket="b", prefix="calls/", count=10)
    assert [o["Key"] for o in out] == ["calls/a.mp3"]


def test_the_count_limits_what_comes_back():
    client = _S3([{"Contents": [_obj(f"{i}.mp3", when=T1) for i in range(10)]}])
    assert len(s3_client._list_objects(client, bucket="b", prefix="", count=3)) == 3


def test_pages_are_all_consumed():
    client = _S3([
        {"Contents": [_obj("a.mp3", when=T1)]},
        {"Contents": [_obj("b.mp3", when=T2)]},
    ])
    out = s3_client._list_objects(client, bucket="b", prefix="", count=10)
    assert [o["Key"] for o in out] == ["b.mp3", "a.mp3"]


def test_an_empty_bucket_lists_nothing():
    assert s3_client._list_objects(_S3([{}]), bucket="b", prefix="", count=5) == []


def test_the_prefix_is_passed_to_the_provider():
    client = _S3([{}])
    s3_client._list_objects(client, bucket="b", prefix="calls/2026/", count=5)
    assert client.paginator.calls == [{"Bucket": "b", "Prefix": "calls/2026/"}]


def test_a_missing_prefix_becomes_an_empty_one():
    client = _S3([{}])
    s3_client._list_objects(client, bucket="b", prefix="", count=5)
    assert client.paginator.calls[0]["Prefix"] == ""


# ── S3: metadata ────────────────────────────────────────────────────────────
def test_s3_metadata_has_the_six_keys_the_pipeline_reads():
    client = _S3([], metadata={"calls/a.mp3": {
        "caller": "Fictional Caller", "phone": "01000 000000",
        "agent": "Reception", "duration": "3:21",
    }})
    meta = s3_client._to_meta(client, "bucket", _obj("calls/a.mp3", when=T3))
    assert meta == {
        "ext_id": "calls/a.mp3",
        "caller": "Fictional Caller",
        "phone": "01000 000000",
        "agent": "Reception",
        "date": "2026-11-20",
        "duration": "3:21",
    }


def test_s3_metadata_falls_back_when_the_object_carries_none():
    client = _S3([], metadata={})
    meta = s3_client._to_meta(client, "bucket", _obj("a.mp3", when=T1))
    assert meta["caller"] == "Unknown Caller"
    assert meta["agent"] == "Agent"
    assert meta["phone"] == ""
    assert meta["duration"] == ""
    assert meta["ext_id"] == "a.mp3"


def test_a_denied_head_object_still_yields_usable_metadata():
    """One unreadable object must not stop the whole listing."""
    client = _S3([], head_raises=True)
    meta = s3_client._to_meta(client, "bucket", _obj("a.mp3", when=T2))
    assert meta["caller"] == "Unknown Caller"
    assert meta["date"] == "2026-11-19"


def test_listing_uses_head_not_get_so_a_known_call_is_never_downloaded():
    """The reason list and transcribe are split: dedup happens before download."""
    client = _S3([], metadata={})
    s3_client._to_meta(client, "bucket", _obj("a.mp3", when=T1))
    assert client.heads == ["a.mp3"]


def test_a_bucket_is_required():
    with pytest.raises(ValueError):
        s3_client.list_latest(
            5, endpoint="https://s3.example.test", region="us-east-1", bucket="",
            access_key_id="k", secret_access_key="s",
        )


def test_s3_list_latest_maps_every_object(monkeypatch):
    client = _S3([{"Contents": [_obj("a.mp3", when=T2), _obj("b.mp3", when=T3)]}])
    monkeypatch.setattr(s3_client, "_client", lambda **_kw: client)
    out = s3_client.list_latest(
        5, endpoint="https://s3.example.test", region="us-east-1", bucket="b",
        access_key_id="k", secret_access_key="s",
    )
    assert [m["ext_id"] for m in out] == ["b.mp3", "a.mp3"]
    assert all(set(m) == {"ext_id", "caller", "phone", "agent", "date", "duration"}
               for m in out)


# ── BT: endpoint normalisation ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "given,expected",
    [
        ("platform.ringcentral.com", "https://platform.ringcentral.com"),
        ("https://platform.ringcentral.com", "https://platform.ringcentral.com"),
        ("https://platform.ringcentral.com/restapi/v1.0", "https://platform.ringcentral.com"),
        ("http://localhost:8080/path", "http://localhost:8080"),
    ],
)
def test_the_bt_endpoint_is_reduced_to_scheme_and_host(given, expected):
    assert bt_client._server(given) == expected


@pytest.mark.parametrize("given", ["", "/just/a/path"])
def test_an_endpoint_without_a_host_is_refused(given):
    with pytest.raises(ValueError):
        bt_client._server(given)


def test_a_whitespace_only_endpoint_is_not_caught_here():
    """Recording actual behaviour, not endorsing it.

    `"   "` has no scheme, so it becomes `https://   `, whose netloc is
    non-empty, so `_server` accepts it and the nonsense URL fails later at the
    HTTP call rather than at configuration time.

    It is unreachable through the configured path: `source_config.load()`
    strips `RC_SERVER_URL` and lists it as missing when the result is empty, so
    a whitespace-only setting is refused before `bt_client` is reached
    (`test_source_config.py` covers that). Pinned here so the belt-and-braces
    gap is visible rather than assumed closed -- and left alone rather than
    "fixed", since changing it would be altering production behaviour to suit a
    test.
    """
    assert bt_client._server("   ") == "https://   "


# ── BT: metadata ────────────────────────────────────────────────────────────
def _rec(**over):
    rec = {
        "id": 99001,
        "direction": "Inbound",
        "from": {"name": "Fictional Caller", "phoneNumber": "+441000000000"},
        "to": {"name": "Reception", "extensionNumber": "101"},
        "startTime": "2026-11-20T09:15:00.000Z",
        "duration": 252,
        "recording": {"id": "r-1", "contentUri": "https://example.test/r-1/content"},
    }
    rec.update(over)
    return rec


def test_an_inbound_call_names_the_outside_party_as_the_caller():
    meta = bt_client._to_meta(_rec())
    assert meta["caller"] == "Fictional Caller"
    assert meta["phone"] == "+441000000000"
    assert meta["agent"] == "Reception"


def test_an_outbound_call_swaps_the_parties():
    """Who called whom decides which side is 'the caller'."""
    meta = bt_client._to_meta(_rec(direction="Outbound"))
    assert meta["caller"] == "Reception"
    assert meta["agent"] == "Fictional Caller"


def test_the_duration_is_rendered_as_the_display_string():
    assert bt_client._to_meta(_rec(duration=252))["duration"] == "4:12"
    assert bt_client._to_meta(_rec(duration=30))["duration"] == "0:30"
    assert bt_client._to_meta(_rec(duration=0))["duration"] == "0:00"
    assert bt_client._to_meta(_rec(duration=None))["duration"] == "0:00"


def test_the_duration_round_trips_through_the_crm_parser():
    """The display string is parsed back by crm_sync; the two must agree."""
    from app import crm_sync

    for seconds in (0, 30, 59, 60, 252, 3599):
        rendered = bt_client._to_meta(_rec(duration=seconds))["duration"]
        assert crm_sync._duration_seconds(rendered) == seconds


def test_the_date_comes_from_the_start_time():
    assert bt_client._to_meta(_rec())["date"] == "2026-11-20"


def test_a_missing_start_time_falls_back_to_today():
    assert bt_client._to_meta(_rec(startTime=""))["date"] == date.today().isoformat()
    assert bt_client._to_meta(_rec(startTime="2026"))["date"] == date.today().isoformat()


def test_a_nameless_party_falls_back_to_its_number_then_to_a_placeholder():
    meta = bt_client._to_meta(_rec(**{"from": {"phoneNumber": "+441000000001"}}))
    assert meta["caller"] == "+441000000001"

    meta = bt_client._to_meta(_rec(**{"from": {}, "to": {}}))
    assert meta["caller"] == "Unknown Caller"
    assert meta["agent"] == "Agent"


def test_the_content_uri_is_carried_so_audio_is_fetched_only_when_needed():
    assert bt_client._to_meta(_rec())["content_uri"] == "https://example.test/r-1/content"


def test_an_id_is_always_produced():
    assert bt_client._to_meta(_rec())["ext_id"] == "99001"
    assert bt_client._to_meta(_rec(id=None))["ext_id"] == "r-1"
    fallback = bt_client._to_meta(_rec(id=None, recording={}))["ext_id"]
    assert fallback and len(fallback) == 32, "a uuid, so dedup still has a key"


# ── BT: token refresh ───────────────────────────────────────────────────────
def test_a_stale_token_is_refreshed_once_and_the_call_retried(monkeypatch):
    tokens = iter(["stale", "fresh"])
    forced: list[bool] = []

    def fake_get_token(*_a, force=False, **_kw):
        forced.append(force)
        return next(tokens)

    monkeypatch.setattr(bt_client, "_get_token", fake_get_token)

    seen: list[str] = []

    def fn(token):
        seen.append(token)
        if token == "stale":
            raise httpx.HTTPStatusError(
                "unauthorised", request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(401),
            )
        return "ok"

    assert bt_client._with_token_retry("https://s", "id", "secret", "jwt", fn) == "ok"
    assert seen == ["stale", "fresh"]
    assert forced == [False, True], "the retry must force a new token"


def test_a_non_401_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(bt_client, "_get_token", lambda *_a, **_kw: "tok")
    attempts: list[str] = []

    def fn(token):
        attempts.append(token)
        raise httpx.HTTPStatusError(
            "server error", request=httpx.Request("GET", "https://example.test"),
            response=httpx.Response(500),
        )

    with pytest.raises(httpx.HTTPStatusError):
        bt_client._with_token_retry("https://s", "id", "secret", "jwt", fn)
    assert len(attempts) == 1, "a 500 is not a token problem"


def test_a_401_twice_propagates(monkeypatch):
    monkeypatch.setattr(bt_client, "_get_token", lambda *_a, **_kw: "tok")

    def fn(_token):
        raise httpx.HTTPStatusError(
            "unauthorised", request=httpx.Request("GET", "https://example.test"),
            response=httpx.Response(401),
        )

    with pytest.raises(httpx.HTTPStatusError):
        bt_client._with_token_retry("https://s", "id", "secret", "jwt", fn)


# ── BT: the demo path ───────────────────────────────────────────────────────
def test_no_jwt_yields_demo_recordings_rather_than_an_error(monkeypatch):
    monkeypatch.setattr(bt_client, "_list_real", lambda *a, **kw: pytest.fail("called live"))
    out = bt_client.list_latest(3, endpoint="platform.ringcentral.com",
                                client_id="id", client_secret="secret", jwt="")
    assert len(out) == 3


def test_demo_recordings_carry_a_canned_transcript(monkeypatch):
    """So the pipeline skips transcription for them -- it checks this key."""
    out = bt_client._fetch_demo(2)
    assert all(item["transcript"] for item in out)
    assert all(set(item) >= {"ext_id", "caller", "phone", "agent", "date", "duration"}
               for item in out)


def test_demo_ids_are_distinct_so_dedup_does_not_collapse_them():
    out = bt_client._fetch_demo(8)
    assert len({item["ext_id"] for item in out}) == 8


def test_a_jwt_takes_the_live_path(monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(bt_client, "_list_real",
                        lambda count, **_kw: called.append(count) or [])
    bt_client.list_latest(4, endpoint="platform.ringcentral.com",
                          client_id="id", client_secret="secret", jwt="a-jwt")
    assert called == [4]


def test_a_demo_duration_is_in_the_display_shape():
    for _ in range(20):
        value = bt_client._rand_duration()
        minutes, _, seconds = value.partition(":")
        assert minutes.isdigit() and len(seconds) == 2 and seconds.isdigit()
        assert int(seconds) < 60
