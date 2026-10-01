"""Raw /v1/search — ``searxng_empty`` degraded only when SearXNG ran.

An absent or circuit-broken SearXNG is a configuration/health state, not an
empty lane — the degraded counter must not tick for it.
"""

from types import SimpleNamespace


class _FakeRegistry:
    def __init__(self, providers) -> None:
        self._providers = providers

    def get(self, name):
        return self._providers.get(name)

    def spec(self, name):
        return SimpleNamespace(timeout_s=5.0, target_latency_ms=100.0)


class _FakeMonitor:
    def allow(self, name):
        from core.provider_health import Admission

        return Admission.allow


class _FakeExecutor:
    def __init__(self, monitor, calls) -> None:
        self.calls = calls

    async def call_provider(self, name, provider, sq, ctx, **kwargs):
        self.calls.append(name)
        return [], None


def _orchestrator(providers):
    from core.query_understanding import QueryUnderstanding

    return SimpleNamespace(
        query_understanding=QueryUnderstanding(),
        registry=_FakeRegistry(providers),
        monitor=_FakeMonitor(),
    )


def _post_raw_search():
    import main as app_module
    from fastapi.testclient import TestClient

    return TestClient(app_module.app).post(
        "/v1/search", json={"query": "văn bản hợp đồng", "type": "web", "max_results": 3}
    )


def test_searxng_empty_not_counted_when_not_invoked(monkeypatch):
    """SearXNG unregistered → never called → no searxng_empty degraded tick."""
    import api.v1 as v1
    import core.federation as fed

    degraded: list[str] = []
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: _orchestrator({}))
    monkeypatch.setattr(v1, "observe_degraded", degraded.append)
    monkeypatch.setattr(fed, "FederatedExecutor", lambda monitor: _FakeExecutor(monitor, []))

    resp = _post_raw_search()
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    assert "searxng_empty" not in degraded


def test_searxng_empty_counted_when_called_and_empty(monkeypatch):
    import api.v1 as v1
    import core.federation as fed

    calls: list[str] = []
    degraded: list[str] = []
    monkeypatch.setattr(
        v1,
        "_get_orchestrator",
        lambda: _orchestrator({"searxng": SimpleNamespace()}),
    )
    monkeypatch.setattr(v1, "observe_degraded", degraded.append)
    monkeypatch.setattr(fed, "FederatedExecutor", lambda monitor: _FakeExecutor(monitor, calls))

    resp = _post_raw_search()
    assert resp.status_code == 200
    assert calls == ["searxng"]  # no DDGS fake registered → single lane
    assert degraded == ["searxng_empty"]
