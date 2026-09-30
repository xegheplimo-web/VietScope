# syntax=docker/dockerfile:1

# ── Builder: resolve + install deps from the frozen lock ─────────────
FROM python:3.12-slim AS builder

# uv binary only — version pinned for reproducible builds
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

WORKDIR /app

# Toolchain for any sdists without wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Lockfile is the source of truth — fails the build if it drifts
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --compile-bytecode

# ── Runtime: slim, non-root, no toolchain ────────────────────────────
FROM python:3.12-slim

# curl is needed by HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

RUN groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir -p /app/.local/share && chown -R app:app /app/.local

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app . .

ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8888/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
