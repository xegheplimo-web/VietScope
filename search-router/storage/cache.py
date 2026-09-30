"""Redis-backed TTL cache with JSON (de)serialization (SPEC-v3 §13).

Values are stored as JSON strings in a small self-describing envelope.
Pydantic ``BaseModel`` instances round-trip back to the same model type via
``model_dump_json`` / ``model_validate_json``; everything else uses plain
``json`` serialization.
"""

import json
import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_ENVELOPE_MODEL = "model"
_ENVELOPE_JSON = "json"


def _import_type(type_name: str) -> type | None:
    module_name, _, attr = type_name.rpartition(".")
    try:
        module = __import__(module_name, fromlist=[attr])
        return getattr(module, attr)
    except Exception:
        return None


def serialize(value: Any) -> str:
    """Encode a value into a JSON envelope string."""
    if isinstance(value, BaseModel):
        envelope = {
            "t": _ENVELOPE_MODEL,
            "n": f"{type(value).__module__}.{type(value).__qualname__}",
            "d": value.model_dump_json(),
        }
    else:
        envelope = {"t": _ENVELOPE_JSON, "d": json.dumps(value, default=str)}
    return json.dumps(envelope)


def deserialize(raw: str) -> Any:
    """Decode a JSON envelope string back into the original value."""
    envelope = json.loads(raw)
    if envelope.get("t") == _ENVELOPE_MODEL:
        model_cls = _import_type(envelope.get("n", ""))
        if model_cls is not None and issubclass(model_cls, BaseModel):
            return model_cls.model_validate_json(envelope.get("d", "null"))
        return json.loads(envelope.get("d", "null"))
    return json.loads(envelope.get("d", "null"))


class RedisCache:
    """TTL cache backed by a Redis client, keyed under a shared prefix."""

    def __init__(self, client, prefix: str = "sh", namespace: str = ""):
        self._client = client
        if prefix and namespace:
            self._prefix = f"{prefix}:{namespace}"
        else:
            self._prefix = prefix or "sh"

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _pattern(self) -> str:
        return f"{self._prefix}:*"

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(self._key(key))
        if raw is None:
            return None
        try:
            return deserialize(raw)
        except Exception as exc:
            logger.warning("Failed to deserialize cache value for %s: %s", key, exc)
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        await self._client.set(self._key(key), serialize(value), ex=ttl)

    async def delete(self, key: str) -> int:
        return await self._client.delete(self._key(key))

    async def clear(self) -> None:
        async for key in self._client.scan_iter(match=self._pattern()):
            await self._client.delete(key)

    async def stats(self) -> dict:
        size = 0
        async for _ in self._client.scan_iter(match=self._pattern()):
            size += 1
        return {"size": size, "backend": "redis", "prefix": self._prefix}
