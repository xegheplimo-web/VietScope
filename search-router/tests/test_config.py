"""Settings/env-driven config tests (hardening adopted from search-hub-clean)."""

from pathlib import Path

from config import Settings, settings

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]


def test_cors_origins_default_dev_frontends(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_cors_origins_parsed_from_env(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", " https://a.example ,https://b.example ,, ")
    assert Settings().cors_origins == ["https://a.example", "https://b.example"]


def test_app_cors_uses_settings_origins():
    import main as app_module
    from fastapi.middleware.cors import CORSMiddleware

    cors = [m for m in app_module.app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "CORSMiddleware not registered on the app"
    assert cors[0].kwargs["allow_origins"] == app_module.settings.cors_origins
    assert "*" not in cors[0].kwargs["allow_origins"]
    # Correlation headers must survive a browser preflight to be usable
    # cross-origin; the middleware echoes X-Request-ID back on responses.
    assert "Search-Id" in cors[0].kwargs["allow_headers"]
    assert "X-Request-ID" in cors[0].kwargs["allow_headers"]
    assert "X-Request-ID" in cors[0].kwargs["expose_headers"]


def test_metrics_db_default_outside_source_tree(monkeypatch):
    monkeypatch.delenv("METRICS_DB_PATH", raising=False)
    path = Path(Settings().metrics_db_path).resolve()
    assert _SEARCH_ROUTER_DIR not in path.parents
    assert path.name == "search_metrics.db"


def test_metrics_db_path_env_override(monkeypatch, tmp_path):
    db = tmp_path / "custom.db"
    monkeypatch.setenv("METRICS_DB_PATH", str(db))
    assert Settings().metrics_db_path == str(db)


def test_metrics_store_default_db_matches_settings():
    import metrics.store as store

    assert store._DB_PATH == Path(settings.metrics_db_path)
    assert _SEARCH_ROUTER_DIR not in store._DB_PATH.resolve().parents
