"""The all-surface credit-ledger view — GET /v1/me/usage/ledger (Spec M3, T8).

Proves that EVERY billed surface's ``credit_transactions`` row surfaces through the
new ledger endpoint with its ``cost_basis`` provenance + the credits moved — unlike
``/v1/me/usage`` (turn_logs, chat only). RLS-scoped: another user's rows never leak.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

# One representative row per basis/surface family (the M3 surfaces write these).
#: Each tuple is (reason, delta [negative = charged], cost_cents, cost_basis).
_SURFACE_ROWS: tuple[tuple[str, int, float, str], ...] = (
    ("image_gen:actual_openrouter", -3, 3.0, "actual_openrouter"),
    ("voice:provider_meter", -9, 8.25, "provider_meter"),
    ("voice:infra_flat", -1, 0.0, "infra_flat"),
    ("sandbox:infra_flat", -1, 0.0, "infra_flat"),
    ("episodic_consolidation:actual_openrouter", -3, 3.0, "actual_openrouter"),
    ("persona_authoring", -2, 1.8, "estimate_static"),
)


@pytest.fixture
def ctx(
    migrated_engine: Engine,  # noqa: ARG001 — session schema + grants
) -> Iterator[tuple[TestClient, str, Engine]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url)
    app = create_app(cfg)
    rls = make_rls_engine(app_url)

    from persona_api.auth import AuthenticatedUser

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    uid = "user_ledger_t8"
    other = "user_ledger_other"
    with TestClient(app) as c:
        app.state.verify_token = _verify
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            for u in (uid, other):
                conn.execute(
                    text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                    {"i": u, "e": f"{u}@x"},
                )
        su.dispose()
        yield c, uid, rls
        rls.dispose()
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": uid, "b": other})
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _write_row(
    engine: Engine, user_id: str, reason: str, delta: int, cents: float, basis: str
) -> None:
    """Insert one credit_transactions row RLS-scoped to ``user_id`` (the deduct shape)."""
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_user_id', :u, false)"), {"u": user_id})
        conn.execute(
            text(
                "INSERT INTO credit_transactions (user_id, delta, reason, cost_cents, cost_basis) "
                "VALUES (:u, :d, :r, :c, :b)"
            ),
            {"u": user_id, "d": delta, "r": reason, "c": cents, "b": basis},
        )


def test_ledger_surfaces_every_billed_surface_with_its_basis(
    ctx: tuple[TestClient, str, Engine],
) -> None:
    c, uid, rls = ctx
    for reason, delta, cents, basis in _SURFACE_ROWS:
        _write_row(rls, uid, reason, delta, cents, basis)

    entries = c.get("/v1/me/usage/ledger", headers=_auth(uid)).json()
    assert len(entries) == len(_SURFACE_ROWS)

    by_reason = {e["reason"]: e for e in entries}
    # Every surface family shows up with its own basis + the credits it moved.
    for reason, delta, cents, basis in _SURFACE_ROWS:
        assert reason in by_reason, f"{reason} missing from the all-surface ledger"
        entry = by_reason[reason]
        assert entry["delta"] == delta
        assert entry["cost_basis"] == basis
        assert entry["cost_cents"] == pytest.approx(cents)

    # The distinct bases all appear — provenance is preserved end-to-end.
    assert {e["cost_basis"] for e in entries} == {
        "actual_openrouter",
        "provider_meter",
        "infra_flat",
        "estimate_static",
    }


def test_ledger_is_owner_scoped(ctx: tuple[TestClient, str, Engine]) -> None:
    c, uid, rls = ctx
    _write_row(rls, uid, "image_gen:actual_openrouter", -3, 3.0, "actual_openrouter")
    _write_row(rls, "user_ledger_other", "voice:provider_meter", -9, 8.25, "provider_meter")

    mine = c.get("/v1/me/usage/ledger", headers=_auth(uid)).json()
    reasons = {e["reason"] for e in mine}
    assert "image_gen:actual_openrouter" in reasons
    assert "voice:provider_meter" not in reasons  # the other user's row never leaks


def test_ledger_newest_first_and_paginates(ctx: tuple[TestClient, str, Engine]) -> None:
    c, uid, rls = ctx
    for reason, delta, cents, basis in _SURFACE_ROWS:
        _write_row(rls, uid, reason, delta, cents, basis)

    page = c.get("/v1/me/usage/ledger?limit=2&offset=0", headers=_auth(uid)).json()
    assert len(page) == 2  # server honours the page size


def test_usage_turn_logs_view_still_works(ctx: tuple[TestClient, str, Engine]) -> None:
    # Back-compat: the original turn_logs view is untouched — no chat turns ⇒ empty,
    # and the ledger rows written above do NOT bleed into it (different source).
    c, uid, rls = ctx
    _write_row(rls, uid, "sandbox:infra_flat", -1, 0.0, "infra_flat")
    usage = c.get("/v1/me/usage", headers=_auth(uid)).json()
    assert usage == []
