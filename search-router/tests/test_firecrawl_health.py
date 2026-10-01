"""firecrawl_health — only a 200 on a probe route means healthy (issue #20).

The previous contract treated 404 as healthy, so any build answering HTTP
at all reported green even with a dead backend behind it.
"""

import asyncio
from types import SimpleNamespace


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _fake_httpx(outcome_by_suffix):
    """Fake ``httpx`` namespace; ``get`` maps URL suffix → status/Exception."""
    seen: list[str] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            seen.append(url)
            for suffix in sorted(outcome_by_suffix, key=len, reverse=True):
                if url.endswith(suffix):
                    outcome = outcome_by_suffix[suffix]
                    if isinstance(outcome, Exception):
                        raise outcome
                    return _Resp(outcome)
            return _Resp(599)

    return SimpleNamespace(AsyncClient=_Client), seen


def test_health_ok_on_200(monkeypatch):
    import providers.firecrawl as fc

    fake, seen = _fake_httpx({"/health": 200})
    monkeypatch.setattr(fc, "httpx", fake)
    assert _run(fc.firecrawl_health()) is True
    assert len(seen) == 1


def test_health_falls_back_to_v2_health(monkeypatch):
    """Builds without ``/health`` still report via the legacy route."""
    import providers.firecrawl as fc

    fake, _ = _fake_httpx({"/health": 404, "/v2/health": 200})
    monkeypatch.setattr(fc, "httpx", fake)
    assert _run(fc.firecrawl_health()) is True


def test_health_404_everywhere_is_not_healthy(monkeypatch):
    """Regression for issue #20: 404s meant 'route missing', not 'healthy'."""
    import providers.firecrawl as fc

    fake, _ = _fake_httpx({"/health": 404, "/v2/health": 404, "/": 404})
    monkeypatch.setattr(fc, "httpx", fake)
    assert _run(fc.firecrawl_health()) is False


def test_health_root_is_last_liveness_signal(monkeypatch):
    """Pinned v2.11.x build: every named health route 404s; only ``/`` is 200."""
    import providers.firecrawl as fc

    fake, seen = _fake_httpx({"/health": 404, "/v2/health": 404, "/": 200})
    monkeypatch.setattr(fc, "httpx", fake)
    assert _run(fc.firecrawl_health()) is True
    assert len(seen) == 3


def test_health_500_is_not_healthy(monkeypatch):
    import providers.firecrawl as fc

    fake, _ = _fake_httpx({"/health": 500})
    monkeypatch.setattr(fc, "httpx", fake)
    assert _run(fc.firecrawl_health()) is False


def test_health_unreachable_then_root_200(monkeypatch):
    """Connection failure on probes → root reachability decides."""
    import providers.firecrawl as fc

    fake, _ = _fake_httpx({"/health": OSError("refused"), "/": 200})
    monkeypatch.setattr(fc, "httpx", fake)
    assert _run(fc.firecrawl_health()) is True


def test_health_fully_unreachable(monkeypatch):
    import providers.firecrawl as fc

    fake, _ = _fake_httpx({"": OSError("refused")})
    monkeypatch.setattr(fc, "httpx", fake)
    assert _run(fc.firecrawl_health()) is False
