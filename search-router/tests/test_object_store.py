"""Tests for storage/object_store.py — MinIO S3 client (SigV4 over httpx).

A MockTransport fake stands in for MinIO: bucket/object state lives in
memory, so no container is needed.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx
import pytest
from storage.object_store import CollisionError, ObjectStore


class FakeS3:
    """In-memory S3 subset: bucket HEAD/PUT, object PUT/GET."""

    def __init__(self):
        self.buckets: set[str] = set()
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path
        parts = [p for p in path.split("/") if p]

        if path == "/minio/health/live":
            return httpx.Response(200)

        bucket = parts[0] if parts else ""
        key = "/".join(parts[1:])

        if request.method == "HEAD" and not key:
            return httpx.Response(200 if bucket in self.buckets else 404)
        if request.method == "PUT" and not key:
            self.buckets.add(bucket)
            return httpx.Response(200)
        if bucket not in self.buckets:
            return httpx.Response(404, text="NoSuchBucket")
        if request.method == "PUT":
            if request.headers.get("if-none-match") == "*" and key in self.objects:
                return httpx.Response(412, text="PreconditionFailed")
            self.objects[key] = request.content
            return httpx.Response(200)
        if request.method == "GET":
            if key in self.objects:
                return httpx.Response(200, content=self.objects[key])
            return httpx.Response(404, text="NoSuchKey")
        return httpx.Response(405)


def _store(s3: FakeS3 | None = None, **overrides) -> ObjectStore:
    kwargs = {
        "endpoint": "http://minio:9000",
        "access_key": "ak",
        "secret_key": "sk",
        "bucket": "sh-raw-snapshots",
    }
    kwargs.update(overrides)
    if s3 is not None:
        kwargs["client"] = httpx.AsyncClient(transport=httpx.MockTransport(s3.handler))
    return ObjectStore(**kwargs)


def _run(coro):
    return asyncio.run(coro)


def test_unconfigured_degrades():
    store = _store(endpoint="", access_key="", secret_key="")
    assert not store.configured
    assert _run(store.health()) is False
    assert _run(store.put_raw(b"<html>", "k1")) is False
    assert _run(store.get_raw("k1")) is None


def test_health_live():
    s3 = FakeS3()
    assert _run(_store(s3).health()) is True


def test_ensure_bucket_creates_once():
    s3 = FakeS3()
    store = _store(s3)
    assert _run(store.ensure_bucket()) is True
    assert "sh-raw-snapshots" in s3.buckets
    # Cached — no second PUT after the first success.
    puts = [r for r in s3.requests if r.method == "PUT"]
    assert _run(store.ensure_bucket()) is True
    assert len([r for r in s3.requests if r.method == "PUT"]) == len(puts)


def test_put_get_roundtrip():
    s3 = FakeS3()
    store = _store(s3)
    body = b"<html><body>raw snapshot</body></html>"
    assert _run(store.put_raw(body, "raw/doc_abc/2026-09-23.html")) is True
    assert _run(store.get_raw("raw/doc_abc/2026-09-23.html")) == body


def test_get_missing_returns_none():
    s3 = FakeS3()
    store = _store(s3)
    assert _run(store.put_raw(b"x", "a")) is True
    assert _run(store.get_raw("nope")) is None


# ─── F4: conditional create (If-None-Match: *) ────────────────────────────


def test_put_raw_if_none_match_sends_header():
    s3 = FakeS3()
    store = _store(s3)
    assert _run(store.put_raw(b"v", "k1", if_none_match=True)) is True
    puts = [r for r in s3.requests if r.method == "PUT" and r.url.path.endswith("/k1")]
    assert puts and puts[0].headers.get("if-none-match") == "*"


def test_put_raw_if_none_match_412_raises_collision():
    s3 = FakeS3()
    store = _store(s3)
    assert _run(store.put_raw(b"v", "k1", if_none_match=True)) is True
    with pytest.raises(CollisionError):
        _run(store.put_raw(b"v2", "k1", if_none_match=True))
    assert s3.objects["k1"] == b"v"  # the existing snapshot survived


def test_put_raw_without_if_none_match_overwrites():
    s3 = FakeS3()
    store = _store(s3)
    assert _run(store.put_raw(b"v", "k1")) is True
    assert _run(store.put_raw(b"v2", "k1")) is True  # unconditional put
    assert s3.objects["k1"] == b"v2"


def test_requests_are_sigv4_signed():
    s3 = FakeS3()
    store = _store(s3)
    assert _run(store.put_raw(b"data", "k")) is True
    signed = [r for r in s3.requests if "authorization" in {k.lower() for k in r.headers}]
    assert signed, "expected signed requests"
    auth = signed[-1].headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=ak/")
    assert "SignedHeaders=" in auth and "host" in auth
    assert "x-amz-content-sha256" in auth and "x-amz-date" in auth


def test_key_quoting():
    s3 = FakeS3()
    store = _store(s3)
    assert _run(store.put_raw(b"v", "dir/a b#c.html")) is True
    put = [r for r in s3.requests if r.method == "PUT" and "a%20b" in str(r.url)]
    assert put, "key segments must be URI-encoded"
    assert _run(store.get_raw("dir/a b#c.html")) == b"v"


# ─── M3: signed path must equal the path httpx actually transmits ──────────


@pytest.mark.parametrize(
    "key",
    ["a/../b", "../escape", "a/./b", "..", ".", "x/../..", "a/b/.."],
)
def test_dot_segment_keys_rejected(key):
    """httpx normalizes `.`/`..` segments before sending — signing the raw
    path would produce a signature for a different request target."""
    s3 = FakeS3()
    store = _store(s3)
    with pytest.raises(ValueError):
        _run(store.put_raw(b"x", key))
    with pytest.raises(ValueError):
        _run(store.get_raw(key))


def test_signed_path_matches_wire_path():
    """Every signed request: canonical URI == path httpx puts on the wire."""
    s3 = FakeS3()
    store = _store(s3)
    signed_paths: list[str] = []
    orig = store._signed_headers

    def spy(method, path, payload, extra_headers):
        signed_paths.append(path)
        return orig(method, path, payload, extra_headers)

    store._signed_headers = spy
    assert _run(store.put_raw(b"v", "dir/a b#c.html")) is True
    assert _run(store.get_raw("dir/a b#c.html")) == b"v"

    wire_paths = [urlparse(str(r.url)).path for r in s3.requests]
    assert signed_paths == wire_paths
