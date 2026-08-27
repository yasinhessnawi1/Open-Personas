# Open Persona — `persona-connectors` production image (Specs C0–C5).
#
# Standalone connector service. `python -m persona_connectors` runs ONE process
# that does two things at once:
#   - an HTTP server on :8080 — inbound webhooks (Twilio, Postmark, Telegram
#     webhook-mode) + OAuth callbacks (Discord/Slack account-linking) + Slack
#     HTTP events. It binds :8080 only when an HTTP-serving connector is
#     configured (Twilio/Postmark/Slack-HTTP/Telegram-webhook).
#   - persistent OUTBOUND clients — Discord gateway (WebSocket), Slack socket
#     mode (WebSocket), Telegram long-poll — which need no inbound URL.
# It shares the api's Postgres (conversation + link-token store); no local state,
# no volume, no HF models.

# ---------- builder ----------
FROM python:3.12-slim AS builder
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
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

WORKDIR /app
# Workspace pyproject + lock first for layer caching.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/runtime/pyproject.toml packages/runtime/pyproject.toml
COPY packages/api/pyproject.toml packages/api/pyproject.toml
COPY packages/voice/pyproject.toml packages/voice/pyproject.toml
COPY packages/connectors/pyproject.toml packages/connectors/pyproject.toml
COPY packages/ packages/
# Frozen install of the whole workspace (matches api/voice images).
RUN uv sync --frozen --all-packages --no-dev

# ---------- runtime ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /bin/bash persona

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

WORKDIR /app
USER persona

EXPOSE 8080
CMD ["python", "-m", "persona_connectors"]
