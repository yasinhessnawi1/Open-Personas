"""V14-T5b — lossless bidirectional voice-provider migration (review finding I1's fix).

Runs against real Postgres + real RLS. Proves the two properties the review flagged
as missing from the T5a auto-remap:

1. **Lossless restore**: a persona previously remapped away from a provider (its
   original voice remembered along the way, via ``identity.voice_by_provider``)
   gets that EXACT voice back on flip-back — not a fresh model pick, not the
   shared default.
2. **RLS-scoped, no cross-tenant bleed**: :func:`reconcile_voice_assignments`
   walks personas across EVERY tenant (the cross-tenant sweep-engine read), but
   each persona's write lands ONLY on that persona's own row, scoped via the
   ``current_user_id`` contextvar per iteration (the DeadLegSweeper pattern) —
   two different owners' mismatched personas are each restored to THEIR OWN
   remembered voice, never the other's.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` fixture param used only for schema setup ordering.
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
import yaml as pyyaml
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import voice_assignment_service as vas
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration


@pytest.fixture
def rls_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    token = current_user_id.set(None)  # start with no ambient scope — reconcile sets it per row
    try:
        yield engine
    finally:
        current_user_id.reset(token)
        engine.dispose()


def _yaml_with_memory(*, voice: str, voice_by_provider: dict[str, str]) -> str:
    return pyyaml.safe_dump(
        {
            "schema_version": "1.0",
            "identity": {
                "name": "Astrid",
                "role": "assistant",
                "background": "A helpful assistant.",
                "language_default": "en",
                "voice": voice,
                "voice_by_provider": voice_by_provider,
            },
        },
        sort_keys=False,
    )


def _seed(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('owner_a', 'a@example.com')"))
        conn.execute(text("INSERT INTO users (id, email) VALUES ('owner_b', 'b@example.com')"))
        # owner_a's persona: currently elevenlabs-voiced, remembers a DIFFERENT
        # cartesia voice from before an earlier flip.
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('persona_a', 'owner_a', :y)"),
            {
                "y": _yaml_with_memory(
                    voice="elevenlabs:current-el-voice",
                    voice_by_provider={
                        "cartesia": "owner-a-original-cartesia-voice",
                        "elevenlabs": "current-el-voice",
                    },
                )
            },
        )
        # owner_b's persona: also elevenlabs-voiced, remembers a DIFFERENT
        # cartesia voice — proves no bleed between tenants.
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('persona_b', 'owner_b', :y)"),
            {
                "y": _yaml_with_memory(
                    voice="elevenlabs:owner-b-el-voice",
                    voice_by_provider={
                        "cartesia": "owner-b-original-cartesia-voice",
                        "elevenlabs": "owner-b-el-voice",
                    },
                )
            },
        )


def _read_voice(migrated_engine: Engine, persona_id: str) -> str:
    with migrated_engine.begin() as conn:
        row = conn.execute(
            text("SELECT yaml FROM personas WHERE id = :id"), {"id": persona_id}
        ).first()
    assert row is not None
    parsed = pyyaml.safe_load(row[0])
    voice = parsed["identity"]["voice"]
    assert isinstance(voice, str)
    return voice


class TestReconcileRestoresLosslesslyAcrossTenants:
    def test_two_tenants_each_restore_their_own_remembered_cartesia_voice(
        self, migrated_engine: Engine, rls_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(migrated_engine)
        # The catalogue fetch is network I/O — scripted (out of scope for this
        # RLS/persistence-focused test; the model-pick path is a unit-test concern).
        monkeypatch.setattr(vas, "_fetch_catalogue", _aret_cartesia_empty)

        counts = _run_reconcile(migrated_engine, rls_engine)

        assert counts == {"scanned": 2, "remapped": 2, "skipped": 0, "failed": 0}
        # Each persona restored to ITS OWN remembered cartesia voice — no bleed.
        assert (
            _read_voice(migrated_engine, "persona_a") == "cartesia:owner-a-original-cartesia-voice"
        )
        assert (
            _read_voice(migrated_engine, "persona_b") == "cartesia:owner-b-original-cartesia-voice"
        )

    def test_restore_also_preserves_the_elevenlabs_memory_for_a_future_flip(
        self, migrated_engine: Engine, rls_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set_voice's choke point merges (never replaces) voice_by_provider — so
        after restoring cartesia, the persona STILL remembers its elevenlabs
        voice too (a future flip forward restores THAT exactly)."""
        _seed(migrated_engine)
        monkeypatch.setattr(vas, "_fetch_catalogue", _aret_cartesia_empty)

        _run_reconcile(migrated_engine, rls_engine)

        with migrated_engine.begin() as conn:
            row = conn.execute(text("SELECT yaml FROM personas WHERE id = 'persona_a'")).first()
        assert row is not None
        parsed = pyyaml.safe_load(row[0])
        by_provider = parsed["identity"]["voice_by_provider"]
        assert by_provider["cartesia"] == "owner-a-original-cartesia-voice"
        assert by_provider["elevenlabs"] == "current-el-voice"  # untouched, still remembered


async def _aret_cartesia_empty(*_a: object, **_k: object) -> tuple[str, list[object]]:
    return "cartesia", []


def _run_reconcile(migrated_engine: Engine, rls_engine: Engine) -> dict[str, int]:
    import asyncio
    from types import SimpleNamespace

    config = SimpleNamespace(
        voice_service_url="http://voice",
        voice_pick_tier="small",
        voice_tts_provider="cartesia",
    )
    return asyncio.run(
        vas.reconcile_voice_assignments(
            config=config,
            registry=SimpleNamespace(get=lambda _t: object()),  # never reached (restore path)
            free_tier_registry=None,  # plan gating is not what this test exercises
            sweep_engine=migrated_engine,  # the RLS-bypassing cross-tenant read
            rls_engine=rls_engine,
        )
    )
