"""Dev-only environment bootstrap for running api/voice from a source checkout.

The pydantic ``Settings`` read ``os.environ`` only (no ``env_file``), so a plain
``uvicorn`` sees nothing. The ``persona_api.dev`` / ``persona_voice.dev`` entrypoints
call :func:`load_local_env` BEFORE importing the app, making the repo-root ``.env``
the single source of truth: an agent adds a ``PERSONA_*`` var to ``.env`` and it is
picked up on the next restart — no launcher edits, nothing "missing".

Never imported by the app itself or by the test suites (which import ``persona_api.app``
/ ``persona_voice.http.app`` directly), so test isolation is untouched — ``.env`` is
loaded only when you explicitly run a ``*.dev`` entrypoint.
"""

from __future__ import annotations

import os
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _repo_root() -> Path | None:
    """The checkout root — the first ancestor carrying a ``.env`` or ``.git``."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists() or (parent / ".git").exists():
            return parent
    return None


def _swap_userinfo(dsn: str, user: str, password: str) -> str:
    """``dsn`` with its user:password replaced, host/port/db/query preserved.

    Used to derive the ``persona_app`` (RLS role) DSN from the owner ``DATABASE_URL``
    so the app and voice can share one Neon host without duplicating it in ``.env``.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(dsn)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def load_local_env() -> None:
    """Load the repo-root ``.env`` + a few dev-derived vars into ``os.environ``.

    Idempotent and safe outside a checkout (no ``.env`` ⇒ the load is a no-op).
    ``override=False`` throughout: an explicitly-exported var always wins over
    ``.env``, so ``PERSONA_FOO=x uvicorn persona_api.dev:create_app`` still works.
    """
    root = _repo_root()

    # 1) the .env file — the single source of truth for every PERSONA_* var.
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]
    if load_dotenv is not None and root is not None and (root / ".env").exists():
        load_dotenv(root / ".env", override=False)

    # 1a) Derive the app/voice DB URLs + voice LiveKit creds from the primaries, so
    #     .env only needs DATABASE_URL (Neon dev DB) + PERSONA_LIVEKIT_* (your real
    #     LiveKit) — no local docker Postgres or LiveKit sidecar. The cloud-edition
    #     guard requires a NON-superuser app DSN, so APP_DATABASE_URL is DATABASE_URL
    #     with the owner creds swapped for the ``persona_app`` RLS role (created by
    #     scripts/dev-bootstrap.sh). Voice keeps the owner DSN (it filters by explicit
    #     owner_id and does not set the RLS GUC — the run-local precedent).
    db = os.environ.get("DATABASE_URL")
    if db:
        app_pw = os.environ.get("PERSONA_APP_DB_PASSWORD", "persona_app")
        os.environ.setdefault("APP_DATABASE_URL", _swap_userinfo(db, "persona_app", app_pw))
        os.environ.setdefault("PERSONA_VOICE_DATABASE_URL", db)
    for suffix in ("URL", "API_KEY", "API_SECRET"):
        real = os.environ.get(f"PERSONA_LIVEKIT_{suffix}")
        if real:
            os.environ.setdefault(f"PERSONA_VOICE_LIVEKIT_{suffix}", real)

    # 2) macOS outbound TLS: the framework Python ignores the system keychain, so
    #    STT/TTS websockets fail CERTIFICATE_VERIFY_FAILED and a call hangs on
    #    "Listening". Point SSL at certifi's CA bundle (prod images carry ca-certs).
    if "SSL_CERT_FILE" not in os.environ:
        try:
            import certifi

            os.environ["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass

    # 3) Clerk JWT public key from the dev secret — the pem is the source of truth
    #    that matches the running Clerk instance (keeps the multi-line key out of .env).
    if root is not None:
        pem = root / "packages" / "api" / ".secrets" / "clerk-jwt-public.pem"
        if pem.exists():
            key = pem.read_text()
            # The pem is the dev source of truth — it matches the running Clerk
            # instance. OVERRIDE any (stale) PERSONA_*_JWT_PUBLIC_KEY that .env may
            # carry (run-local's precedent: it exported the pem, unconditionally).
            # A mismatched .env key silently 401s every request.
            os.environ["PERSONA_API_JWT_PUBLIC_KEY"] = key
            os.environ["PERSONA_VOICE_JWT_PUBLIC_KEY"] = key

    # 4) optional cheap-dev tiers (PERSONA_DEV_CHEAP_TIERS=true): force every tier to
    #    deepseek-chat for cheap iteration. Sets the MODELS lists, which win over the
    #    per-tier triplet (D-20-17). Off by default ⇒ the real .env tiers are used.
    if os.environ.get("PERSONA_DEV_CHEAP_TIERS", "").strip().lower() in _TRUE:
        for tier in ("FRONTIER", "MID", "SMALL"):
            os.environ[f"PERSONA_{tier}_MODELS"] = "deepseek/deepseek-chat"
