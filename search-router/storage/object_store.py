"""MinIO raw snapshot object store (Phase 1/T2).

Async S3 client over ``httpx`` with in-module AWS Signature V4 signing — no
SDK dependency. Path-style addressing (``{endpoint}/{bucket}/{key}``) is what
MinIO serves on the compose network (no wildcard DNS for vhost-style).

Degrade-by-design, same contract as ``storage.pg_client`` / ``redis_client``:
when ``MINIO_ENDPOINT`` or credentials are unset, or MinIO is unreachable,
every method returns ``False``/``None`` instead of raising.

Wired into ``crawler.pipeline``: every fetched body is persisted here
first (``snapshots/<domain>/<content-hash>/<uuid>.bin``), and
``document_snapshots.storage_key`` points at the blob before extraction
runs — an extraction failure can never lose the raw capture.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx
from config import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 15.0
_HEALTH_TIMEOUT_S = 2.0


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _quote_key(key: str) -> str:
    # S3 canonical URI: encode each segment, keep "/" separators.
    return quote(key, safe="/-_.~")


def _object_key_path(key: str) -> str:
    """Validate + quote a storage key for path-style addressing.

    httpx normalizes ``.``/``..`` path segments before sending, so signing
    the un-normalized path would produce a signature for a different request
    target than the wire path (SigV4 mismatch). Reject such keys outright.
    """
    if any(segment in (".", "..") for segment in key.split("/")):
        raise ValueError(f"object key contains a dot-segment: {key!r}")
    return _quote_key(key)


class CollisionError(Exception):
    """``put_raw(if_none_match=True)`` hit an existing key (HTTP 412)."""


class ObjectStore:
    """Minimal async S3 client for the raw-snapshot bucket.

    ``client`` is injectable for tests (e.g. ``httpx.MockTransport``); when
    not provided a short-lived ``httpx.AsyncClient`` is created per call.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        region: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = (endpoint if endpoint is not None else settings.minio_endpoint).rstrip("/")
        self._access_key = access_key if access_key is not None else settings.minio_access_key
        self._secret_key = secret_key if secret_key is not None else settings.minio_secret_key
        self._bucket = bucket or settings.minio_bucket_raw
        self._region = region or settings.minio_region
        self._client = client
        self._bucket_ready = False

    @property
    def configured(self) -> bool:
        return bool(self._endpoint and self._access_key and self._secret_key)

    @property
    def bucket(self) -> str:
        return self._bucket

    # ── Health / bucket bootstrap ────────────────────────────────────────

    async def health(self) -> bool:
        """Unsigned probe — MinIO ``/minio/health/live`` needs no auth."""
        if not self._endpoint:
            return False
        try:
            resp = await self._request_raw(
                "GET",
                f"{self._endpoint}/minio/health/live",
                signed=False,
                timeout=_HEALTH_TIMEOUT_S,
            )
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001 — probe must not raise
            logger.debug("minio health probe failed: %r", exc)
            return False

    async def ensure_bucket(self) -> bool:
        """Create the raw bucket when missing. Cached per instance."""
        if not self.configured:
            return False
        if self._bucket_ready:
            return True
        try:
            head = await self._request_raw("HEAD", self._bucket_path())
            if head.status_code == 404:
                created = await self._request_raw("PUT", self._bucket_path())
                if created.status_code not in (200, 409):
                    logger.warning("minio bucket create failed: HTTP %s", created.status_code)
                    return False
            elif head.status_code != 200:
                logger.warning("minio bucket head failed: HTTP %s", head.status_code)
                return False
            self._bucket_ready = True
            return True
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            logger.warning("minio ensure_bucket failed: %r", exc)
            return False

    # ── Raw snapshot API ─────────────────────────────────────────────────

    async def put_raw(
        self,
        data: bytes,
        storage_key: str,
        content_type: str = "application/octet-stream",
        *,
        if_none_match: bool = False,
    ) -> bool:
        """Store raw bytes under ``storage_key`` in the raw bucket.

        With ``if_none_match=True`` the PUT is a conditional create
        (``If-None-Match: *``): an existing key answers 412, surfaced as
        ``CollisionError`` so the caller can pick a fresh key — a blind
        overwrite would silently destroy another fetch's snapshot.

        Raises ``ValueError`` for keys httpx would rewrite on the wire
        (dot-segments) — caller input error, not infra degrade.
        """
        if not self.configured or not storage_key or data is None:
            return False
        path = self._object_path(storage_key)
        if not await self.ensure_bucket():
            return False
        extra = {"content-type": content_type}
        if if_none_match:
            extra["if-none-match"] = "*"
        try:
            resp = await self._request_raw("PUT", path, body=data, extra_headers=extra)
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            logger.warning("minio put_raw failed: %r", exc)
            return False
        if resp.status_code == 412:
            raise CollisionError(f"key already exists: {storage_key}")
        if resp.status_code != 200:
            logger.warning("minio put failed: HTTP %s", resp.status_code)
            return False
        return True

    async def get_raw(self, storage_key: str) -> bytes | None:
        """Fetch raw bytes for ``storage_key``; None when absent/failed.

        Raises ``ValueError`` for dot-segment keys (same reason as put_raw).
        """
        if not self.configured or not storage_key:
            return None
        path = self._object_path(storage_key)
        try:
            resp = await self._request_raw("GET", path)
            if resp.status_code == 200:
                return resp.content
            if resp.status_code != 404:
                logger.warning("minio get failed: HTTP %s", resp.status_code)
            return None
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            logger.warning("minio get_raw failed: %r", exc)
            return None

    # ── SigV4 plumbing ───────────────────────────────────────────────────

    def _bucket_path(self) -> str:
        return f"/{self._bucket}"

    def _object_path(self, key: str) -> str:
        return f"/{self._bucket}/{_object_key_path(key)}"

    def _signed_headers(
        self, method: str, path: str, payload: bytes, extra_headers: dict[str, str] | None
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()

        headers: dict[str, str] = {
            "host": urlparse(self._endpoint).netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amzdate,
        }
        if extra_headers:
            headers.update({k.lower(): v for k, v in extra_headers.items()})

        canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
        signed_headers = ";".join(sorted(headers))
        canonical_request = "\n".join(
            [method, path, "", canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{datestamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amzdate,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signing_key = _hmac_sha256(("AWS4" + self._secret_key).encode(), datestamp)
        signing_key = _hmac_sha256(signing_key, self._region)
        signing_key = _hmac_sha256(signing_key, "s3")
        signing_key = _hmac_sha256(signing_key, "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    async def _request_raw(
        self,
        method: str,
        url: str,
        *,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        signed: bool = True,
        timeout: float = _REQUEST_TIMEOUT_S,
    ) -> httpx.Response:
        if url.startswith("/"):
            url = f"{self._endpoint}{url}"
        headers: dict[str, str] = {}
        if signed:
            path = urlparse(url).path
            # Invariant: the canonical URI we sign must be the exact path
            # httpx transmits. httpx normalizes dot-segments, so a signed
            # path that differs from the wire path always yields an invalid
            # signature — fail loudly instead of sending a doomed request.
            wire_path = httpx.URL(url).raw_path.decode("ascii")
            if wire_path != path:
                raise ValueError(
                    f"sigv4 signed path {path!r} diverges from httpx wire path "
                    f"{wire_path!r} — refusing to send a mis-signed request"
                )
            headers = self._signed_headers(method, path, body, extra_headers)
        if self._client is not None:
            return await self._client.request(method, url, content=body, headers=headers)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, content=body, headers=headers)


# Convenience accessor matching the codebase's lazy-singleton style.
_STORE: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _STORE
    if _STORE is None:
        _STORE = ObjectStore()
    return _STORE
