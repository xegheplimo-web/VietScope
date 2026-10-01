"""Compose mount contract: host bind-mount sources used by default-profile
services must exist in a fresh clone.

Regression guard for the P0.4 finding — `firecrawl-postgres` mounted
`./firecrawl/postgres-init/01-postgis.sql`, a path the firecrawl submodule
does not track. On a fresh volume Docker creates a *directory* at the mount
target; the postgres entrypoint `*.sql` glob then feeds that directory to
psql, the container exits 1, and `firecrawl-api` never starts behind its
`service_healthy` dependency. A long-lived volume skips initdb entirely,
which is why the break only surfaces on a fresh clone/volume.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
INITDB_DIR = "/docker-entrypoint-initdb.d"
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _load_services() -> dict:
    # A parse failure raises here — the test fails loudly instead of
    # silently passing on an unreadable compose file.
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(compose, dict), f"{COMPOSE_FILE} did not parse to a mapping"
    services = compose.get("services")
    assert isinstance(services, dict) and services, f"{COMPOSE_FILE} defines no services"
    return services


def _iter_mounts(service: dict):
    """Yield (source, target) pairs for every mount entry of one service."""
    for entry in service.get("volumes") or []:
        if isinstance(entry, str):
            parts = entry.split(":")
            # Drive-letter source ("C:\\src:/dst[:opts]") splits into 3+ parts.
            if len(parts) >= 3 and _WINDOWS_ABS.match(parts[0] + ":" + parts[1]):
                source, target = parts[0] + ":" + parts[1], parts[2]
            elif len(parts) >= 2:
                source, target = parts[0], parts[1]
            else:
                continue  # anonymous volume: container path only
            if source and target:
                yield source, target
        elif isinstance(entry, dict):
            if entry.get("type") not in (None, "bind"):
                continue  # named volume, tmpfs, npipe — not a host path
            source = entry.get("source") or entry.get("src")
            target = entry.get("target") or entry.get("dst") or entry.get("destination")
            if source and target:
                yield str(source), str(target)


def _is_bind_source(source: str) -> bool:
    """True when the source is a host path rather than a named volume."""
    return source.startswith(("./", "../", ".\\", "..\\", "/", "~", "\\")) or bool(
        _WINDOWS_ABS.match(source)
    )


def _resolve(source: str) -> Path:
    path = Path(source).expanduser()
    return path if path.is_absolute() else REPO_ROOT / source


def test_default_profile_bind_mount_sources_exist():
    missing = []
    for name, svc in _load_services().items():
        if svc.get("profiles"):
            continue  # profile-gated: not started by `docker compose up -d`
        for source, target in _iter_mounts(svc):
            if not _is_bind_source(source):
                continue  # named volume — not a repo path
            if not _resolve(source).exists():
                missing.append(f"  {name}: {source} -> {target}")
    assert not missing, (
        "default-profile services bind-mount sources missing from the checkout:\n"
        + "\n".join(missing)
    )


def test_firecrawl_postgres_has_no_initdb_bind_mount():
    services = _load_services()
    fc_postgres = services.get("firecrawl-postgres")
    assert fc_postgres is not None, "firecrawl-postgres service missing from compose"
    assert "volumes" not in fc_postgres, (
        "firecrawl-postgres must not declare volumes — the removed postgres-init "
        "bind mount pointed at a path the firecrawl submodule does not track"
    )
    offenders = []
    for name, svc in services.items():
        for source, target in _iter_mounts(svc):
            norm_source = source.replace("\\", "/").removeprefix("./")
            under_initdb = target == INITDB_DIR or target.startswith(INITDB_DIR + "/")
            under_firecrawl = norm_source == "firecrawl" or norm_source.startswith("firecrawl/")
            if under_initdb and under_firecrawl:
                offenders.append(f"  {name}: {source} -> {target}")
    assert not offenders, (
        "no service may seed /docker-entrypoint-initdb.d/ from firecrawl/:\n" + "\n".join(offenders)
    )
