# Open Persona — `persona-api` production image (Spec 11, T10)
#
# Single uvicorn worker (S08-4 / D-08-5): the in-process agentic-run event bus
# and the in-memory rate limiter require it. Migrations run once per deploy via
# the fly.toml release_command (packages/api/release.py) — NOT on container
# start (Spec 07 §7) and not by hand; a release failure aborts the deploy.
# On the server deploy kit, its one-shot `migrate` service runs release.py (bin/deploy.sh).
#
# Builds natively on linux/amd64 and linux/arm64 (the Oracle server is arm64):
# torch comes from PyTorch's CPU index on Linux (uv.lock), and nothing installed here
# lacks an arm64 wheel (scripts/tests/test_api_image.py).

# ---------- builder ----------
# python:3.12-slim pinned by its multi-arch index digest (amd64, arm64; read 2026-11-07),
# in both stages: bump it deliberately, for security updates (scripts/tests/test_api_image.py).
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS builder
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# uv's download ceiling. The 30s default is fine on a fast native builder and not
# fine on an emulated cross-build or a slow link: torch and pydantic-core are big
# enough that one transient stall fails the entire sync. Raises the ceiling only;
# a healthy download is unaffected.
ENV UV_HTTP_TIMEOUT=300
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /usr/local/bin/uv

WORKDIR /app
# Copy workspace pyproject + lock first to leverage the layer cache.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/runtime/pyproject.toml packages/runtime/pyproject.toml
COPY packages/api/pyproject.toml packages/api/pyproject.toml
COPY packages/voice/pyproject.toml packages/voice/pyproject.toml
COPY packages/connectors/pyproject.toml packages/connectors/pyproject.toml
COPY packages/ packages/
# Only the api process's closure (frozen): persona-connectors depends on persona-api
# (C1-D-1) and runs inside it (the I1 fold), so this installs connectors, api, runtime
# and core[postgres], and NOT persona-voice, which the api never imports (it reaches voice
# over HTTP). Every native package here has an arm64 wheel, so the image builds on arm64
# (the Oracle box) and amd64 alike.
# The cache mount keeps uv's download cache out of the image layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package persona-connectors

# ---------- runtime ----------
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
# R9-041: `git` — the MCP catalog mirror sync (mirror_sync.py) and the skill
# sources sync (skill_sources_sync.py) both shell out to `git clone --depth 1`
# to refresh from their upstream registries; without it on PATH they degrade
# fail-soft (WARN + keep the existing mirror) rather than crash, but that
# degrade was previously guaranteed on every deploy of this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /bin/bash persona

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

WORKDIR /app
USER persona

# Audit / turnlog JSONL roots — bind-mount a volume in production.
ENV PERSONA_AUDIT_ROOT=/var/lib/persona/audit \
    PERSONA_TURNLOG_PATH=/var/lib/persona/turnlogs

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Single uvicorn worker (S08-4). Set proxy headers because the VPS terminates TLS upstream.
CMD ["uvicorn", "persona_api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
