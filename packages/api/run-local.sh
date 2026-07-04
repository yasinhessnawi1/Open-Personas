#!/usr/bin/env bash
# Run the persona-api (:8000) AND persona-voice (:8001) locally for web dev.
#   - DB: the `persona-pg` Docker container on :5436 (superuser + persona_app RLS role)
#   - Auth: Clerk RS256, verified against the dashboard PEM + the `persona-api` aud
#   - Model: DeepSeek (cheap) for all tiers, key sourced from the repo-root .env
#   - Voice (Spec V6): persona-voice on :8001 with the in-process agent worker;
#     needs the LiveKit dev sidecar (`docker compose up -d livekit`) + STT/TTS
#     keys (PERSONA_STT_API_KEY / PERSONA_TTS_API_KEY) in ../../.env.
# No secrets live in this file; keys come from ../../.env (gitignored).
set -euo pipefail
cd "$(dirname "$0")"

# Developer keys (DeepSeek PERSONA_API_KEY, web-search, etc.) from the repo root.
if [ -f ../../.env ]; then set -a; . ../../.env; set +a; fi

# CA bundle for outbound TLS. The macOS Python.framework doesn't use the system
# keychain, so the agent's STT (Deepgram) + TTS (Cartesia) websockets fail with
# "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate" and the
# call hangs on "Listening". Point Python's SSL at certifi's bundle. (Prod uses
# a container image that carries ca-certificates — the V5 note; this is the
# host-dev equivalent.)
export SSL_CERT_FILE="${SSL_CERT_FILE:-$(uv run python -m certifi 2>/dev/null)}"

# Spec 33 open-core editions: this script wires the full COMMERCIAL stack
# (Postgres + RLS + Clerk auth + credits), so it must run as the `cloud` edition.
# Without this the API defaults to `community` (file-based SQLite + Chroma) while
# the voice agent still loads personas from Postgres → a persona created by the
# API can't be found by voice ("persona not found"), and avatars/memory land in
# the wrong store. Both api + voice read PERSONA_EDITION (no prefix).
export PERSONA_EDITION="cloud"

# The single local process IS the whole cluster (D-08-5): without the in-process
# worker the durable A0 consumer + A1 scheduler tick never run, so synthesis jobs
# pile up unclaimed (graph/memory facts are never extracted → "the persona doesn't
# remember") AND scheduled tasks never fire. Prod runs a dedicated worker; local
# must turn it on here or those two subsystems are silently dead. (R4-C1-11)
export PERSONA_API_IN_PROCESS_WORKER="true"

export DATABASE_URL="postgresql+psycopg://persona:persona@localhost:5436/persona"
export APP_DATABASE_URL="postgresql+psycopg://persona_app:persona_app@localhost:5436/persona"

# --- Idempotent local DB bootstrap ------------------------------------------
# A fresh or wiped `pgdata` volume comes up with NO schema, and even after a
# migration the app role (`persona_app`) has NO grants — so the cloud API 500s
# on the first request ("relation ... does not exist"). Both steps below are
# safe to run on every launch:
#   1. `alembic upgrade head` — a no-op when already at head.
#   2. grant `persona_app` — GRANT / ALTER DEFAULT PRIVILEGES are idempotent.
# Gated on a reachability probe so a stopped/remote Postgres just warns + skips
# rather than aborting the launch. Runs as the superuser DATABASE_URL.
if uv run python - <<'PY'
import os, sys
from sqlalchemy import create_engine, text
try:
    with create_engine(os.environ["DATABASE_URL"]).connect() as c:
        c.execute(text("SELECT 1"))
except Exception as exc:  # noqa: BLE001 — best-effort probe
    print(f"[run-local] Postgres unreachable, skipping DB bootstrap: {exc}", file=sys.stderr)
    sys.exit(1)
PY
then
  echo "[run-local] DB bootstrap: alembic upgrade head + persona_app grants…"
  uv run alembic upgrade head
  uv run python - <<'PY'
import os
from sqlalchemy import create_engine, text
GRANTS = [
    "GRANT USAGE ON SCHEMA public TO persona_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_app",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_app",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO persona_app",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO persona_app",
]
with create_engine(os.environ["DATABASE_URL"]).begin() as c:
    if c.execute(text("SELECT 1 FROM pg_roles WHERE rolname='persona_app'")).first():
        for stmt in GRANTS:
            c.execute(text(stmt))
        print("[run-local] persona_app grants applied.")
    else:
        print("[run-local] role persona_app absent; skipped grants.")
PY
  echo "[run-local] DB bootstrap complete."
fi

# Clerk JWT verification (D-09-2): RS256 + the dashboard PEM + the template aud.
export PERSONA_API_JWT_ALGORITHMS="RS256"
export PERSONA_API_JWT_AUDIENCE="persona-api"
export PERSONA_API_JWT_PUBLIC_KEY="$(cat .secrets/clerk-jwt-public.pem)"

# Force ALL tiers → DeepSeek (cheap) for spec-09 dev. The repo .env sets per-tier
# providers (frontier=anthropic, small=groq); those take precedence over the
# single PERSONA_PROVIDER fallback, so each tier must be overridden explicitly.
# The DeepSeek key is read from whichever var holds it.
DEEPSEEK_KEY="${PERSONA_DEEPSEEK_API_KEY:-${PERSONA_MID_API_KEY:-${PERSONA_API_KEY:-}}}"
for TIER in FRONTIER MID SMALL; do
  export "PERSONA_${TIER}_PROVIDER=deepseek"
  export "PERSONA_${TIER}_MODEL=deepseek-chat"
  export "PERSONA_${TIER}_API_KEY=${DEEPSEEK_KEY}"
done
export PERSONA_PROVIDER="deepseek"
export PERSONA_MODEL="deepseek-chat"
export PERSONA_API_KEY="${DEEPSEEK_KEY}"

# --- Spec V6: persona-voice service (:8001) ---------------------------------
# The voice call path (POST /v1/voice/token + GET /v1/voices) and the in-process
# agent worker. Same Clerk JWT as the API (the web sends one token to both →
# same RS256 key + persona-api aud); deepseek tiers + STT/TTS keys from above;
# LiveKit dev creds match the docker-compose sidecar (`LIVEKIT_KEYS: "devkey: secret"`).
#
# DB: the SUPERUSER role (not persona_app). The token endpoint's ownership +
# credits checks query with explicit owner_id/user_id filters, but they do NOT
# set the `app.current_user_id` GUC the persona_app RLS policies require — so
# under persona_app every row is hidden ("persona not found"). The superuser
# bypasses RLS; the explicit filters keep tenant isolation. The RLS-aware engine
# (ContextVar + checkout listener, like persona-api D-08-1) is the proper fix for
# the production voice deploy (a forward-item).
export PERSONA_VOICE_DATABASE_URL="${DATABASE_URL}"
export PERSONA_VOICE_JWT_ALGORITHMS="RS256"
export PERSONA_VOICE_JWT_AUDIENCE="persona-api"
export PERSONA_VOICE_JWT_PUBLIC_KEY="$(cat .secrets/clerk-jwt-public.pem)"
export PERSONA_VOICE_LIVEKIT_URL="${PERSONA_VOICE_LIVEKIT_URL:-ws://localhost:7880}"
export PERSONA_VOICE_LIVEKIT_API_KEY="${PERSONA_VOICE_LIVEKIT_API_KEY:-devkey}"
export PERSONA_VOICE_LIVEKIT_API_SECRET="${PERSONA_VOICE_LIVEKIT_API_SECRET:-secret}"
export PERSONA_VOICE_AGENT_INPROCESS="true"
export PERSONA_VOICE_CORS_ORIGINS="${PERSONA_VOICE_CORS_ORIGINS:-http://localhost:3000}"

# Start persona-voice in the background; run persona-api in the foreground.
# The trap stops the voice service when the api exits (Ctrl-C).
uv run uvicorn persona_voice.http.app:create_app --factory --host 127.0.0.1 --port 8001 &
VOICE_PID=$!
trap 'kill "${VOICE_PID}" 2>/dev/null || true' EXIT INT TERM

uv run uvicorn persona_api.app:create_app --factory --host 127.0.0.1 --port 8000
