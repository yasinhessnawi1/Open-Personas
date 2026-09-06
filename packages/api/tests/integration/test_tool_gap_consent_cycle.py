"""Cross-spec gap → consent → retry cycle + backward-compat (spec 26 T13).

End-to-end against real Postgres, proving the runtime-availability half of the
loop the unit tests can't (the DB round-trip):

1. A persona that does NOT declare ``calculator`` builds a runtime toolbox that
   does NOT advertise it (gap precondition).
2. ``grant_tool_consent`` persists the tool to the YAML column (T11).
3. A freshly-built toolbox (persona reloaded from the DB) now advertises +
   dispatches ``calculator`` — the consented tool actually became usable. This
   is the "→ retry succeeds" half of acceptance criterion #4.

Backward-compat: a persona declaring only pre-spec-26 tools advertises EXACTLY
those — the new built-ins are registered but never leak into an allow-list that
didn't ask for them (acceptance criterion #7).
"""

# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.schema.tools import ToolCall
from persona.tools._factory import SELF_KNOWLEDGE_TOOLS
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import persona_service, tool_consent_service
from persona_api.services.runtime_factory import RuntimeFactory
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_YAML_NO_CALC = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: A helper for the gap-consent cycle test.
tools:
  - web_search
"""


class _FakeBackend:
    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("not used")


class _FakeTierRegistry:
    def get(self, tier_name: str) -> _FakeBackend:
        return _FakeBackend()


def _factory(engine: Engine, embedder: HashEmbedder384, audit_root: Path) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=engine,
        embedder=embedder,
        tier_registry=_FakeTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=audit_root,
    )


@pytest.mark.asyncio
async def test_gap_consent_retry_cycle(
    migrated_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    su_url = migrated_engine.url.render_as_string(hide_password=False)
    engine = make_rls_engine(su_url)
    owner = "user_gap_cycle"
    audit_root = tmp_path / "audit"

    token = current_user_id.set(owner)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": owner, "e": f"{owner}@x"},
            )
        persona_id = persona_service.create_persona(
            rls_engine=engine,
            embedder=embedder,
            audit_root=audit_root,
            owner_id=owner,
            yaml_str=_YAML_NO_CALC,
        )
        factory = _factory(engine, embedder, audit_root)

        # 1. Gap precondition: calculator is NOT advertised; web_search is.
        persona1 = factory._load_persona(persona_id)  # type: ignore[attr-defined]
        tb1 = await factory._build_toolbox(persona1, scanned_skills=[])  # type: ignore[attr-defined]
        assert "calculator" not in tb1.names()  # type: ignore[attr-defined]
        assert "web_search" in tb1.names()  # type: ignore[attr-defined]

        # 2. Consent grant (persists to the YAML column).
        granted = tool_consent_service.grant_tool_consent(
            rls_engine=engine,
            embedder=embedder,
            audit_root=audit_root,
            persona_id=persona_id,
            owner_id=owner,
            tool_name="calculator",
            written_by=owner,
            now=datetime.now(UTC),
            turn_index=1,
        )
        assert granted is True

        # 3. Retry: a freshly-built toolbox (persona reloaded) advertises +
        #    dispatches calculator — the consented tool is now usable.
        persona2 = factory._load_persona(persona_id)  # type: ignore[attr-defined]
        tb2 = await factory._build_toolbox(persona2, scanned_skills=[])  # type: ignore[attr-defined]
        assert "calculator" in tb2.names()  # type: ignore[attr-defined]
        result = await tb2.dispatch(  # type: ignore[attr-defined]
            ToolCall(name="calculator", args={"expression": "17 * 19"}, call_id="c1")
        )
        assert result.is_error is False
        assert result.content == "323"
    finally:
        current_user_id.reset(token)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
        engine.dispose()


@pytest.mark.asyncio
async def test_backward_compat_only_declared_tools_advertised(
    migrated_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    su_url = migrated_engine.url.render_as_string(hide_password=False)
    engine = make_rls_engine(su_url)
    owner = "user_bc_cycle"
    audit_root = tmp_path / "audit"
    yaml_old = (
        'schema_version: "1.0"\n'
        "identity:\n  name: Old\n  role: assistant\n  background: pre-spec-26 persona.\n"
        "tools:\n  - web_search\n  - file_read\n"
    )

    token = current_user_id.set(owner)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": owner, "e": f"{owner}@x"},
            )
        persona_id = persona_service.create_persona(
            rls_engine=engine,
            embedder=embedder,
            audit_root=audit_root,
            owner_id=owner,
            yaml_str=yaml_old,
        )
        factory = _factory(engine, embedder, audit_root)
        persona = factory._load_persona(persona_id)  # type: ignore[attr-defined]
        tb = await factory._build_toolbox(persona, scanned_skills=[])  # type: ignore[attr-defined]
        # Exactly the declared tools are advertised, no spec-26 built-in leaks in.
        # The self-knowledge tools (R9-075: schedule_introspect, use_skill) are
        # auto-allowed by design and never named in a YAML allow-list, so they are
        # subtracted rather than expected: their presence depends on composition,
        # not on the persona.
        advertised = set(tb.names()) - SELF_KNOWLEDGE_TOOLS  # type: ignore[attr-defined]
        assert advertised == {"web_search", "file_read"}
    finally:
        current_user_id.reset(token)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
        engine.dispose()
