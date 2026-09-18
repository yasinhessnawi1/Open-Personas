"""A run that breaks says WHERE it broke (R9-180).

``StepType.ERROR`` existed from spec 06 and nothing ever produced one. An unrecoverable
failure left the loop as an exception, so the record kept a run-level message and nothing
else: the step list stopped at the last thing that worked, and a run opened afterwards
could not point at the step it died on.

These drive the real failure chain, a toolbox that raises, a model call that raises, a
tier that resolves to nothing, through ``AgenticLoop.run``, and pin the three things
that have to agree: the run's status, the run's error text, and the closing step.
"""

# ruff: noqa: SLF001 (tests reach into the registry cache to pin the scripted backend)

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from _fakes import FakeStore, ScriptedBackend  # type: ignore[import-not-found]
from persona.backends import BackendConfig, ChatResponse
from persona.backends.types import TokenUsage
from persona.errors import GatedActionProposedError
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall, ToolResult
from persona.skills import SkillInjector
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.run import RunStatus
from persona_runtime.agentic.step import MemoryRecallNote, StepType
from persona_runtime.errors import TierNotConfiguredError
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import Run

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


@tool(name="web_search", description="Search the web.")
async def _web_search(query: str) -> ToolResult:
    return ToolResult(tool_name="web_search", content=f"nothing about {query}")


def _resp(content: str = "", *, tool_calls: list[ToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="claude-sonnet-4-6",
        provider="anthropic",
        latency_ms=1.0,
    )


def _search(query: str, call_id: str) -> ChatResponse:
    return _resp(tool_calls=[ToolCall(name="web_search", args={"query": query}, call_id=call_id)])


class _ExplodingBackend(ScriptedBackend):  # type: ignore[misc]
    """A backend whose Nth call fails the way a provider does."""

    def __init__(self, script: list[ChatResponse], *, fail_on: int, exc: Exception) -> None:
        super().__init__([], chat_script=script)
        self._fail_on = fail_on
        self._exc = exc
        self._attempts = 0

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        self._attempts += 1
        if self._attempts == self._fail_on:
            raise self._exc
        return await super().chat(*args, **kwargs)


class _UnreadableStore(FakeStore):  # type: ignore[misc]
    """A typed store whose read fails, which is how the initial context can blow up."""

    def query(self, _pid: str, _query: str, _top_k: int, **_filters: Any) -> list[Any]:  # noqa: ANN401
        raise RuntimeError("the store is unreachable")


class _RaisingToolbox(Toolbox):
    """A toolbox whose dispatch fails with something the loop does not translate.

    ``@tool`` bodies never raise (spec 03 turns a failure into an ``is_error`` result the
    model recovers from), so a tool failure that actually ENDS a run comes from the
    toolbox layer, which is where the policy-gated one lives too.
    """

    def __init__(self, tools: list[Any], *, exc: Exception) -> None:
        super().__init__(tools, allow_list=None)
        self._exc = exc

    async def dispatch(self, _call: ToolCall, **_kwargs: Any) -> ToolResult:  # noqa: ANN401
        raise self._exc


def _make_loop(
    script: list[ChatResponse],
    *,
    backend: ScriptedBackend | None = None,
    toolbox: Toolbox | None = None,
    tiers: dict[str, TierConfig] | None = None,
) -> AgenticLoop:
    stores: dict[str, FakeStore] = {
        "identity": FakeStore(),
        "self_facts": FakeStore(),
        "worldview": FakeStore(),
        "episodic": FakeStore(),
    }
    chat_backend = backend or ScriptedBackend([], chat_script=script)
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
        if tiers is None
        else tiers
    )

    if tiers is None:
        registry._cache = {  # type: ignore[assignment]
            "frontier": chat_backend,
            "mid": chat_backend,
            "small": chat_backend,
        }
    return AgenticLoop(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(name="Astrid", role="researcher", background="bg"),
        ),
        stores=stores,  # type: ignore[arg-type]
        toolbox=toolbox or Toolbox([_web_search], allow_list=None),  # type: ignore[arg-type]
        skill_injector=SkillInjector(),
        scanned_skills=[],
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
    )


async def _run(loop: AgenticLoop) -> tuple[Run, list[RunEvent]]:
    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    run = await loop.run("find the deposit rules", on_event=on_event)
    return run, events


@pytest.mark.asyncio
async def test_a_tool_that_blows_up_ends_the_run_on_an_error_step() -> None:
    """THE regression: the record has to name the step the run died on."""
    script = [_search("husleieloven", "c1"), _resp("[FINAL] never reached")]
    loop = _make_loop(
        script,
        toolbox=_RaisingToolbox([_web_search], exc=RuntimeError("the sandbox is gone")),
    )

    run, _events = await _run(loop)

    assert run.status is RunStatus.ERROR
    last = run.steps[-1]
    assert last.type is StepType.ERROR
    # The two places the failure appears must say the same thing, or the step points at
    # one story while the run page tells another.
    assert last.content == run.error
    assert "the sandbox is gone" in str(run.error)
    assert run.error_class == "RuntimeError"


@pytest.mark.asyncio
async def test_the_error_step_lands_at_the_step_that_failed() -> None:
    """Two steps of real work, then the failure, all three stay on the record."""
    backend = _ExplodingBackend(
        [_search("one", "c1"), _search("two", "c2")],
        fail_on=3,
        exc=RuntimeError("provider 503"),
    )
    loop = _make_loop([], backend=backend)

    run, events = await _run(loop)

    assert [s.type for s in run.steps] == [
        StepType.TOOL_CALL,
        StepType.TOOL_CALL,
        StepType.ERROR,
    ]
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1
    # The live stream points at the same step the record does.
    assert errors[0].step == len(run.steps) - 1
    assert errors[0].data["message"] == run.error


@pytest.mark.asyncio
async def test_the_run_finishes_as_error_rather_than_raising() -> None:
    """Every terminal outcome is a status (D-06-2), and this one is no different."""
    backend = _ExplodingBackend([], fail_on=1, exc=RuntimeError("connection reset"))
    loop = _make_loop([], backend=backend)

    run, events = await _run(loop)

    assert run.status is RunStatus.ERROR
    assert run.output is None
    assert [e for e in events if e.type == "finished"][-1].data["status"] == "error"


@pytest.mark.asyncio
async def test_a_tier_that_resolves_to_nothing_is_recorded_not_raised() -> None:
    """The other everyday failure: a tier name with no backend behind it."""
    loop = _make_loop([], tiers={})

    run, _events = await _run(loop)

    assert run.status is RunStatus.ERROR
    assert run.error_class == TierNotConfiguredError.__name__
    assert run.steps[-1].type is StepType.ERROR


@pytest.mark.asyncio
async def test_a_failed_run_writes_nothing_to_episodic_memory() -> None:
    """A run that broke has no outcome to remember."""
    backend = _ExplodingBackend([], fail_on=1, exc=RuntimeError("connection reset"))
    loop = _make_loop([], backend=backend)

    await _run(loop)

    assert loop._stores["episodic"].writes == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_failed_run_still_says_which_memory_it_read() -> None:
    """The two disclosures share step 0, and a failure is when they matter most.

    The recall note (part3 F10) lands on the run's first step, and on a run that broke
    before it made one that step IS the ERROR step. Someone reading a failed run wants
    both halves: what the persona was working from, and where it stopped.
    """
    backend = _ExplodingBackend([], fail_on=1, exc=RuntimeError("connection reset"))
    loop = _make_loop([], backend=backend)

    run, _events = await _run(loop)

    first = run.steps[0]
    assert first.type is StepType.ERROR
    assert [n.store for n in first.notes if isinstance(n, MemoryRecallNote)] == [
        "identity",
        "self_facts",
        "worldview",
        "episodic",
    ]


@pytest.mark.asyncio
async def test_a_store_that_cannot_be_read_is_recorded_like_any_other_failure() -> None:
    """The read that produces the recall notes is itself a thing that can fail."""
    loop = _make_loop([_resp("[FINAL] never reached")])
    loop._stores["worldview"] = _UnreadableStore()  # type: ignore[assignment]

    run, _events = await _run(loop)

    assert run.status is RunStatus.ERROR
    assert run.steps[-1].type is StepType.ERROR
    assert "the store is unreachable" in str(run.error)


@pytest.mark.asyncio
async def test_the_approval_gate_still_travels_untouched() -> None:
    """A gated action means a person to wait for, not a run that failed.

    The leg executor catches this to park the task; recording it as an ERROR step would
    swallow the park and strand the proposal.
    """
    script = [_search("husleieloven", "c1")]
    gated = GatedActionProposedError(
        "approval required",
        context={"proposal_id": "prop_1", "tool": "file_write"},
    )
    loop = _make_loop(script, toolbox=_RaisingToolbox([_web_search], exc=gated))

    with pytest.raises(GatedActionProposedError):
        await _run(loop)
