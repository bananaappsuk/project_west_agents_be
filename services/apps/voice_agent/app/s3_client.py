"""S3-compatible bucket client — fetches call recordings for an organisation.

Covers AWS S3, Backblaze B2, MinIO, Wasabi, Cloudflare R2, and anything else that speaks the
S3 API: the only thing that differs between providers is `endpoint`/`region` (e.g. Backblaze B2
is `https://s3.<region>.backblazeb2.com`; leave `endpoint` blank for AWS S3 itself).

Unlike BT Cloud's call-log API, a raw bucket object has no caller/phone/agent/duration fields of
its own — those are read from the object's S3 user-metadata (`x-amz-meta-caller` etc., set by
whatever uploads the recording) when present, falling back to the same placeholders bt_client.py
uses for missing data otherwise. The object key is the dedup key (`ext_id`); `LastModified` is
the call date. `list_latest` only lists + heads objects (cheap); `transcribe_one` downloads and
transcribes a single object with the same OpenAI transcription step BT Cloud uses (see
transcribe.py) — pipeline.py only calls it for objects not already known, so an already-synced
recording is never re-downloaded on a later sync.
"""

from __future__ import annotations

import logging

import boto3
from botocore.config import Config as BotoConfig

from .transcribe import transcribe

log = logging.getLogger("voice_agent.s3_client")


def _normalize_endpoint(endpoint: str) -> str | None:
    """Providers commonly show their endpoint without a scheme (Backblaze B2's own
    console displays e.g. `s3.us-east-005.backblazeb2.com`), but boto3 requires a full
    URL — add `https://` if the admin pasted it in bare, same as bt_client.py already
    does for its endpoint field."""
    if not endpoint:
        return None
    return endpoint if "://" in endpoint else f"https://{endpoint}"


def _client(*, endpoint: str, region: str, access_key_id: str, secret_access_key: str):
    return boto3.client(
        "s3",
        endpoint_url=_normalize_endpoint(endpoint),
        region_name=region or None,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=BotoConfig(
            signature_version="s3v4",
            # Path-style addressing (bucket.name/key rather than bucket.host/key) is
            # required by MinIO and most non-AWS S3-compatible servers, and still
            # works fine against real AWS S3 — one setting that's safe everywhere.
            s3={"addressing_style": "path"},
            # boto3's defaults (60s connect, 60s read, 'legacy' retries) let one
            # stalled attempt eat a minute-plus before the caller ever finds out —
            # observed hanging 30-70s+ even against a bucket that ultimately
            # connects fine, almost certainly an IPv6-then-IPv4 fallback stall
            # rather than anything wrong with the request itself. Fail an attempt
            # fast and let a couple of quick retries actually land, instead of
            # sitting past the frontend's own 60s request timeout.
            connect_timeout=8,
            read_timeout=20,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _list_objects(client, *, bucket: str, prefix: str, count: int) -> list[dict]:
    paginator = client.get_paginator("list_objects_v2")
    objects: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix or ""):
        objects.extend(page.get("Contents", []))
    # Folder placeholder objects (a key ending in "/") aren't recordings.
    objects = [o for o in objects if not o["Key"].endswith("/")]
    objects.sort(key=lambda o: o["LastModified"], reverse=True)
    return objects[:count]


def _to_meta(client, bucket: str, obj: dict) -> dict:
    """Object metadata only — a `head_object`, not a `get_object` — so a call already known
    to the DB never pays for a full download just to be deduped away afterward."""
    key = obj["Key"]
    meta: dict = {}
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        meta = head.get("Metadata") or {}
    except Exception as exc:
        log.warning("head_object failed for %s: %s", key, exc)

    return {
        "ext_id": key,
        "caller": meta.get("caller") or "Unknown Caller",
        "phone": meta.get("phone") or "",
        "agent": meta.get("agent") or "Agent",
        "date": obj["LastModified"].date().isoformat(),
        "duration": meta.get("duration") or "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API — same shape as bt_client.py so pipeline.py can dispatch by source_type
# ─────────────────────────────────────────────────────────────────────────────

def list_latest(
    count: int, *, endpoint: str, region: str, bucket: str, prefix: str = "",
    access_key_id: str, secret_access_key: str,
) -> list[dict]:
    """Return up to `count` recordings' metadata (no transcript, no download), newest object
    first. An empty or misconfigured bucket just yields zero results — no demo-data fallback
    (unlike BT Cloud, there's no meaningful "demo bucket")."""
    if not bucket:
        raise ValueError("bucket is required")
    client = _client(endpoint=endpoint, region=region, access_key_id=access_key_id, secret_access_key=secret_access_key)
    objects = _list_objects(client, bucket=bucket, prefix=prefix, count=count)
    log.info("s3_client: %d recording(s) from bucket %s", len(objects), bucket)
    return [_to_meta(client, bucket, o) for o in objects]


def transcribe_one(
    item: dict, *, endpoint: str, region: str, bucket: str, access_key_id: str, secret_access_key: str,
) -> str:
    """Download and transcribe a single object — only called for items `list_latest` didn't
    already dedupe away (i.e. never for a call the DB already has)."""
    client = _client(endpoint=endpoint, region=region, access_key_id=access_key_id, secret_access_key=secret_access_key)
    key = item["ext_id"]
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        content_type = obj.get("ContentType") or "audio/mpeg"
        body = obj["Body"].read()
        return transcribe(body, content_type) or "(no transcript available)"
    except Exception as exc:
        log.warning("download/transcribe failed for object %s: %s", key, exc)
        return "(no transcript available)"


def test_connection(
    *, endpoint: str, region: str, bucket: str, prefix: str = "",
    access_key_id: str, secret_access_key: str,
) -> str:
    """Validate the bucket/credentials. Raises on bad input / failed auth / missing bucket."""
    if not bucket:
        raise ValueError("bucket is required")
    if not access_key_id or not secret_access_key:
        raise ValueError("access key ID and secret access key are required")
    client = _client(endpoint=endpoint, region=region, access_key_id=access_key_id, secret_access_key=secret_access_key)
    client.head_bucket(Bucket=bucket)  # raises on missing/forbidden bucket or bad credentials
    return "live"


def upload_object(
    *, endpoint: str, region: str, bucket: str, access_key_id: str, secret_access_key: str,
    key: str, body: bytes, content_type: str,
) -> None:
    """Stores an uploaded recording's audio in the org's S3-compatible bucket so it can
    be played back later the same way an S3-sourced recording can — used by the manual
    upload flow when the user opts in to keeping the audio (see api.py's /recordings/upload)."""
    client = _client(endpoint=endpoint, region=region, access_key_id=access_key_id, secret_access_key=secret_access_key)
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type or "audio/mpeg")


def download(
    *, endpoint: str, region: str, bucket: str, access_key_id: str, secret_access_key: str, key: str,
) -> tuple[bytes, str]:
    """Re-fetch one object's raw bytes + content-type — used to serve playback on
    demand (see api.py's GET /recordings/{id}/audio). Raises on missing object / bad
    credentials; the caller is responsible for turning that into an HTTP error."""
    client = _client(endpoint=endpoint, region=region, access_key_id=access_key_id, secret_access_key=secret_access_key)
    obj = client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    content_type = obj.get("ContentType") or "audio/mpeg"
    return body, content_type
