#!/usr/bin/env bash
# One-time dev DB bootstrap for the database at DATABASE_URL (Neon or local):
#   1. migrate to head (alembic),
#   2. create/refresh the non-superuser `persona_app` RLS role + grants (the
#      cloud-edition guard requires the request path run on this role, not the owner).
#
# Run after a schema change, on a fresh DB, or when you switch DATABASE_URL
# (Neon <-> local). Reads .env directly — the single source of truth — so it
# targets exactly what the app uses. No docker assumptions.
#
#   ./scripts/dev-bootstrap.sh
#
# Then run the services normally (they load .env themselves):
#   cd packages/api   && uv run uvicorn persona_api.dev:create_app     --factory --port 8000 --reload
#   cd packages/voice && uv run uvicorn persona_voice.dev:create_app   --factory --port 8001
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && { set -a; . ./.env; set +a; }
export SSL_CERT_FILE="${SSL_CERT_FILE:-$(uv run python -m certifi 2>/dev/null || true)}"
: "${DATABASE_URL:?DATABASE_URL not set — check .env}"

echo "[dev-bootstrap] alembic upgrade head -> ${DATABASE_URL%%\?*}"
( cd packages/api && uv run alembic upgrade head )

echo "[dev-bootstrap] ensure persona_app RLS role + grants…"
uv run python - <<'PY'
import os
from sqlalchemy import create_engine, text

pw = os.environ.get("PERSONA_APP_DB_PASSWORD", "persona_app")
with create_engine(os.environ["DATABASE_URL"]).begin() as c:
    exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname='persona_app'")).first()
    # NOSONAR — dev bootstrap; pw is a controlled env var, CREATE ROLE can't be parameterised.
    if exists:
        c.execute(text(f"ALTER ROLE persona_app WITH LOGIN PASSWORD '{pw}'"))
    else:
        c.execute(text(f"CREATE ROLE persona_app LOGIN PASSWORD '{pw}'"))
    for stmt in (
        "GRANT USAGE ON SCHEMA public TO persona_app",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_app",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_app",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO persona_app",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO persona_app",
    ):
        c.execute(text(stmt))
    print("[dev-bootstrap] persona_app role + grants applied.")
PY

echo "[dev-bootstrap] done. Start the services with the uvicorn commands above."
