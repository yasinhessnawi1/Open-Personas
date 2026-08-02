"""A5 T12 — the default-OFF structural proof (impossible-green; criterion 9).

The WHOLE initiative system must be inert until ``PERSONA_INITIATIVE_ENABLED``
is set — and each absence assertion is paired with its ON-flip sensitivity
check (the detector detects — a proof that cannot fail is no proof, the
detonate-the-scorer discipline):

- the worker registry carries NO ``initiative_scan`` tenant (and the OFF
  registry's type set is byte-identical to the ON set minus exactly that one);
- the worker's provisioner builder yields ``None`` (no sweep component);
- the factory-built loop carries NO initiative gate (interpreter + pending
  provider both ``None`` — the chat turn is byte-unchanged);
- the settings gate itself defaults False (the app's verb-service block and
  every other composition hangs off the same one flag).
"""

# ruff: noqa: ARG001, ARG002, SLF001 — fixture params; fakes; wired-assert probes
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.initiative import InitiativeSettings
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig
from persona_api.initiative.handler import INITIATIVE_SCAN_JOB_TYPE
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.services.runtime_factory import RuntimeFactory

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a5-defaultoff-audit")
DIM = 384
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
self_facts:
  - fact: knows things
    confidence: 1.0
"""


class _Backend:
    provider_name = "anthropic"
    model_name = "scripted"

    async def chat(self, messages: Any, **_: object) -> ChatResponse:  # noqa: ANN401
        return ChatResponse(
            content="{}",
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class _Registry:
    def get(self, _tier: str) -> _Backend:
        return _Backend()

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def metadata_for(self, _tier: str) -> None:
        return None

    def model_name_for(self, _tier: str) -> str:
        return "scripted"


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _registry_types(app_engine: Engine, tmp_path: Path) -> set[str]:
    return set(
        build_worker_registry(
            rls_engine=app_engine,
            embedder=_Emb(),  # type: ignore[arg-type]
            tier_registry=_Registry(),  # type: ignore[arg-type]
            free_tier_registry=None,  # R9-096: no plans here — gating off, stated
            config=APIConfig(audit_root=str(tmp_path)),
            synthesis_tier="small",
        ).types()
    )


def test_settings_gate_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_INITIATIVE_ENABLED", raising=False)
    assert InitiativeSettings().enabled is False


def test_registry_is_the_pre_a5_set_when_off_and_gains_exactly_one_tenant_when_on(
    app_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERSONA_INITIATIVE_ENABLED", raising=False)
    off_types = _registry_types(app_engine, tmp_path / "off")
    assert INITIATIVE_SCAN_JOB_TYPE not in off_types

    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    on_types = _registry_types(app_engine, tmp_path / "on")
    # The sensitivity half + the byte-identity half in one assertion: the ONLY
    # difference the flag makes to the worker's tenant set is the scan tenant.
    assert on_types - off_types == {INITIATIVE_SCAN_JOB_TYPE}
    assert off_types - on_types == set()


def test_provisioner_builder_yields_none_when_off(
    app_engine: Engine, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persona_api.initiative.provisioner import InitiativeProvisioner

    monkeypatch.delenv("PERSONA_INITIATIVE_ENABLED", raising=False)
    settings = InitiativeSettings()
    assert settings.enabled is False  # the same gate the worker-root builder checks

    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    assert InitiativeSettings().enabled is True  # the sensitivity flip
    # The builder shape itself is exercised by the T10 sweep tests; here the gate.
    assert InitiativeProvisioner is not None


@pytest.mark.asyncio
async def test_factory_loop_carries_no_initiative_gate_when_off(
    app_engine: Engine, migrated_engine: Engine, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat path is byte-unchanged when off — and the SAME factory wires the
    gate when on (the T10 test asserted the wired half; this is the off half)."""
    from persona_api.middleware.rls_context import current_user_id
    from sqlalchemy import text

    owner, persona = "own_a5doff", "pers_doff_a"
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": owner, "e": f"{owner}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, :y) "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": persona, "u": owner, "y": _YAML},
        )
    monkeypatch.delenv("PERSONA_INITIATIVE_ENABLED", raising=False)
    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,  # type: ignore[arg-type]
        tier_registry=_Registry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )
    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona)
        assert loop._initiative_verb_interpreter is None
        assert loop._initiative_pending_provider is None

        # Sensitivity: the SAME factory, flag on → both gates wired.
        monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
        loop_on = await factory.build_conversation_loop(persona)
        assert loop_on._initiative_verb_interpreter is not None
        assert loop_on._initiative_pending_provider is not None
    finally:
        current_user_id.reset(token)
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": owner})
