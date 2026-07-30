"""RuntimeFactory wires ``schedule_introspect`` into the persona's toolbox (R9-075).

The gap this closes is a WIRING gap, not a missing-code gap: a persona could create
schedules and the web calendar could render them, but the toolbox carried no read surface
for either. A tool that exists but never reaches the model changes nothing — so these tests
assert the composed Toolbox actually REGISTERS it (the count moves) and actually ADVERTISES
it (it survives the persona allow-list, which no persona's YAML will ever name it in).

No DB: the factory builds the toolbox with a ``None`` engine, and the reader is resolved
lazily per dispatch from the RLS contextvar (off-request ⇒ ``None`` ⇒ the tool fails closed),
so nothing here touches Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall
from persona_api.services.runtime_factory import RuntimeFactory

_TOOL = "schedule_introspect"


def _make_persona(*, tools: list[str]) -> Persona:
    return Persona(
        persona_id="persona_schedule_wire_test",
        identity=PersonaIdentity(
            name="Astrid",
            role="assistant",
            background="A helper for schedule-wiring tests.",
        ),
        tools=tools,
    )


def _make_factory() -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=None,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-schedule-audit"),
        sandbox_pool=None,
        workspace_root=None,
        image_backend=None,
    )


@pytest.mark.asyncio
async def test_factory_registers_schedule_introspect() -> None:
    """The composition root injects the tool — it is in the toolbox at all."""
    toolbox = await _make_factory()._build_toolbox(  # type: ignore[attr-defined]
        _make_persona(tools=["file_read"]), scanned_skills=[]
    )
    assert _TOOL in toolbox.names()


@pytest.mark.asyncio
async def test_schedule_introspect_survives_an_explicit_allow_list() -> None:
    """Advertised despite a persona allow-list that (like every real one) never names it.

    This is the whole point of the fix. ``ensure_default_capabilities`` guarantees every
    persona carries a non-empty ``tools`` list, so the allow-list is ALWAYS active — a
    registered-but-gated tool would be invisible to the model in production.
    """
    persona = _make_persona(tools=["file_read", "code_execution", "web_search"])
    toolbox = await _make_factory()._build_toolbox(persona, scanned_skills=[])  # type: ignore[attr-defined]

    assert _TOOL not in persona.tools  # the persona never declares it
    assert toolbox.is_allowed(_TOOL)
    assert _TOOL in toolbox.names()
    assert "file_read" in toolbox.names()  # sanity — wires not crossed


@pytest.mark.asyncio
async def test_schedule_introspect_adds_exactly_one_registered_tool() -> None:
    """The registered count moves by one — the tool is genuinely new, not a rename."""
    toolbox = await _make_factory()._build_toolbox(  # type: ignore[attr-defined]
        _make_persona(tools=["file_read"]), scanned_skills=[]
    )
    registered = set(toolbox._tools)  # noqa: SLF001 — the registry IS the assertion
    assert _TOOL in registered
    # The pre-R9-075 composition on this path: 11 build_default_toolbox built-ins +
    # ``task_introspect`` = 12 registered. ``schedule_introspect`` makes 13. (Production
    # adds record_user_fact + the runtime-wired tools whose backends are absent here.)
    assert len(registered) == 13, sorted(registered)


@pytest.mark.asyncio
async def test_schedule_introspect_is_not_a_build_default_builtin() -> None:
    """Absent from ``build_default_toolbox`` — it needs the API's owner-scoped reader.

    Pins where the wiring test belongs: asserting at the core-factory layer would
    false-green, because the tool only exists once the composition root injects it.
    """
    from persona.config import PersonaCoreConfig
    from persona.tools import build_default_toolbox

    toolbox, _ = await build_default_toolbox(
        PersonaCoreConfig(), _make_persona(tools=["file_read"])
    )
    assert _TOOL not in toolbox.names()


@pytest.mark.asyncio
async def test_off_request_dispatch_fails_closed() -> None:
    """No RLS owner bound ⇒ no reader ⇒ the tool refuses instead of answering blind."""
    toolbox = await _make_factory()._build_toolbox(  # type: ignore[attr-defined]
        _make_persona(tools=["file_read"]), scanned_skills=[]
    )
    result = await toolbox.dispatch(
        ToolCall(name=_TOOL, args={"scope": "all"}, call_id="call_schedule_1")
    )

    assert result.is_error is True
    assert "calendar access" in result.content
