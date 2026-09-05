"""Live proof of the RLS role guard against a real Postgres (R9-123).

Reads ``DATABASE_URL`` the way the api integration suite does; that URL is the
privileged ``persona`` role, which the guard must refuse. The ``persona_app`` role is
derived from it with the canonical local password (``run-local.sh``); if that role
cannot connect the second half skips rather than guesses.
"""

from __future__ import annotations

import os

import pytest
from persona_api.middleware.rls_context import make_rls_engine
from persona_connectors.errors import ConnectorError
from persona_connectors.rls_guard import guard_rls_engine_role, verify_rls_engine_role
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

_ADMIN_URL = os.environ.get("DATABASE_URL", "")


def _requires_postgres() -> str:
    if not _ADMIN_URL.startswith("postgresql"):
        pytest.skip("DATABASE_URL (a privileged Postgres role) is not set")
    return _ADMIN_URL


def test_the_privileged_role_is_refused_on_first_connection() -> None:
    engine = make_rls_engine(_requires_postgres(), pool_size=1)
    guard_rls_engine_role(engine)
    try:
        with pytest.raises(ConnectorError, match="bypasses row-level security"):
            verify_rls_engine_role(engine)
    finally:
        engine.dispose()


def test_the_persona_app_role_passes() -> None:
    admin = make_url(_requires_postgres())
    app_url = admin.set(username="persona_app", password="persona_app")
    engine = make_rls_engine(app_url.render_as_string(hide_password=False), pool_size=1)
    guard_rls_engine_role(engine)
    try:
        try:
            verify_rls_engine_role(engine)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 — a missing local role is not this test's subject
            pytest.skip(f"persona_app could not connect here: {type(exc).__name__}")
    finally:
        engine.dispose()
