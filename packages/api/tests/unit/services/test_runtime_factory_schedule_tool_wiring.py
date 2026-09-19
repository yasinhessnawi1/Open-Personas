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
    # ``task_introspect`` = 12 registered. ``schedule_introspect`` makes 13, and Spec W1's
    # ``task_pickup`` 14. Issue 13's write half (``schedule_book_once`` plus
    # ``schedule_remove``) makes 16. (Production adds record_user_fact + the runtime-wired
    # tools whose backends are absent here.)
    assert len(registered) == 16, sorted(registered)


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


@pytest.mark.parametrize("tool_name", ["task_introspect", "task_pickup"])
@pytest.mark.asyncio
async def test_the_persona_can_actually_reach_its_own_work(tool_name: str) -> None:
    """Spec W1 (T9): both halves of the persona's window onto its own tasks are ADVERTISED.

    ``task_introspect`` shipped with A4 and was never auto-allowed, so it hit exactly the
    failure this file was written about: composed, registered, counted in
    ``extra_tool_count``, and then filtered straight back out for every persona with a
    non-empty allow-list — which is every persona, since the default floor is three tools. It
    is absent from the catalog too, so no YAML could name it either. The window a persona had
    onto its own work was never actually open, and "how's the research going?" was answered
    from imagination by a persona that had a grounded answer sitting right there.

    ``task_pickup`` is the write half and would have inherited the same fate.
    """
    persona = _make_persona(tools=["file_read", "code_execution", "web_search"])
    toolbox = await _make_factory()._build_toolbox(persona, scanned_skills=[])  # type: ignore[attr-defined]

    assert tool_name not in persona.tools  # no persona's YAML names it (it is not a capability)
    assert toolbox.is_allowed(tool_name), f"{tool_name} is registered but gated out"
    assert tool_name in toolbox.names()  # and it is actually advertised to the model


# ------------------------------------------------------------------------------------------
# Issue 13: the WRITE half of the same window has to reach the model too.
# ------------------------------------------------------------------------------------------

_WRITE_TOOLS = ["schedule_book_once", "schedule_remove"]


@pytest.mark.parametrize("tool_name", _WRITE_TOOLS)
@pytest.mark.asyncio
async def test_the_persona_can_actually_write_to_the_calendar(tool_name: str) -> None:
    """The defect in issue 13 was the persona having no write door at all.

    A tool composed but gated out would reproduce it exactly: the persona would go on saying
    its scheduling access is read-only, which is why this asserts advertisement through the
    real composition rather than membership of the auto-allow set.
    """
    persona = _make_persona(tools=["file_read", "code_execution", "web_search"])
    toolbox = await _make_factory()._build_toolbox(persona, scanned_skills=[])  # type: ignore[attr-defined]

    assert tool_name not in persona.tools  # no persona's YAML names it
    assert toolbox.is_allowed(tool_name), f"{tool_name} is registered but gated out"
    assert tool_name in toolbox.names()


@pytest.mark.asyncio
async def test_the_read_and_write_tools_arrive_together() -> None:
    """A persona with default permissions gets the whole window: look, book, remove."""
    toolbox = await _make_factory()._build_toolbox(  # type: ignore[attr-defined]
        _make_persona(tools=["file_read"]), scanned_skills=[]
    )
    names = toolbox.names()

    assert {_TOOL, *_WRITE_TOOLS} <= set(names)


@pytest.mark.asyncio
async def test_off_request_booking_fails_closed() -> None:
    """No RLS owner bound ⇒ no booking port ⇒ nothing is written anywhere."""
    toolbox = await _make_factory()._build_toolbox(  # type: ignore[attr-defined]
        _make_persona(tools=["file_read"]), scanned_skills=[]
    )
    result = await toolbox.dispatch(
        ToolCall(
            name="schedule_book_once",
            args={"goal": "Redraw the Gantt chart", "when": "in one hour"},
            call_id="call_book_1",
        )
    )

    assert result.is_error is True
    assert "calendar access" in result.content


@pytest.mark.asyncio
async def test_off_request_removal_fails_closed() -> None:
    """No RLS owner bound ⇒ no removal port and no disclosure record ⇒ nothing is deleted."""
    toolbox = await _make_factory()._build_toolbox(  # type: ignore[attr-defined]
        _make_persona(tools=["file_read"]), scanned_skills=[]
    )
    result = await toolbox.dispatch(
        ToolCall(name="schedule_remove", args={"schedule_id": "sch_1"}, call_id="call_rm_1")
    )

    assert result.is_error is True
    assert "calendar access" in result.content
