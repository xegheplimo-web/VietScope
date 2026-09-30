"""Retry and fallback logic — simple, no framework."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def with_retry(
    func: Callable[..., Awaitable[Any]],
    *args,
    max_retries: int = 2,
    delay: float = 1.0,
    backoff: float = 2.0,
    **kwargs,
) -> Any:
    """Call func with retry on exception.

    Returns result on success, raises last exception after all retries fail.
    """
    last_exc: Exception | None = None
    wait = delay

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(wait)
                wait *= backoff

    raise last_exc  # type: ignore


async def with_fallback(
    primary: Callable[..., Awaitable[Any]],
    fallback: Callable[..., Awaitable[Any]],
    *args,
    **kwargs,
) -> Any:
    """Try primary, fall back to secondary on failure.

    Returns (result, used_fallback: bool).
    """
    try:
        result = await primary(*args, **kwargs)
        # Check if result is "empty" — empty list, empty dict, None
        if _is_empty(result):
            fb_result = await fallback(*args, **kwargs)
            return fb_result, True
        return result, False
    except Exception:
        try:
            fb_result = await fallback(*args, **kwargs)
            return fb_result, True
        except Exception as e:
            raise e


def _is_empty(result: Any) -> bool:
    """Check if a result is considered empty."""
    if result is None:
        return True
    return isinstance(result, (list, dict, str)) and len(result) == 0
