"""The loop's deterministic guards, driven through real runs (Spec W1, T10).

A long run loops. It searches the phrase that returned nothing, re-reads the file it read
four steps ago, and calls the tool that errored with exactly the arguments that made it
error. Every repeat costs a model call, a tool call and a permanent place in the context,
and none of it can produce anything the run does not already have.

Three guards, all proven here through ``AgenticLoop.run`` with a scripted backend rather
than by calling the ledger or the pruner directly, because what matters is that a RUN
stops paying, not that a helper behaves:

- a repeat of a call that ERRORED is not dispatched; the model gets the original error and
  the one instruction that changes anything (D-W1-11);
- a repeat of an allowlisted read that SUCCEEDED is served from the ledger at zero cost,
  and says it is not fresh evidence (D-W1-11);
- once a step's context crosses a COST ceiling, tool results older than the recent steps
  are cut to a bounded head with a marker, so a six-search leg stops compounding
  (D-W1-13). The window-keyed compactor is untouched and never fires here: these backends
  carry a real 128k window, exactly as production does, which is the whole point of the
  ruling.
"""

# ruff: noqa: SLF001 (tests reach into the registry cache to pin the scripted backend)

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _fakes import FakeStore, ScriptedBackend  # type: ignore[import-not-found]
from persona.backends import BackendConfig, ChatResponse
from persona.backends.types import TokenUsage
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall, ToolResult
from persona.skills import SkillInjector, count_tokens
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.agentic.call_ledger import CACHED_RESULT_NOTE, REPEAT_ERROR_HINT
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.pruner import PRUNED_MARKER, ToolResultPruner
from persona_runtime.agentic.step import Step
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import Run

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
#: A real tier's context window. Production never approaches it in a leg, which is why
#: D-W1-13 keys pruning on cost, and pinning it keeps the compactor out of these tests.
_REAL_WINDOW = 128_000


class _Calls:
    """What actually reached the tools, which is the whole question here."""

    def __init__(self) -> None:
        self.searched: list[str] = []
        self.read: list[str] = []
        self.wrote: list[str] = []


def _tools(calls: _Calls, *, body_words: int = 4) -> list[object]:
    """Three tools: an allowlisted read, another allowlisted read, and a write.

    Each answers differently every time it runs, so a cached answer and a fresh one are
    never confusable: if a test sees ``result 1`` twice, the ledger served it.
    """

    @tool(name="web_search", description="Search the web.")
    async def web_search(query: str) -> ToolResult:
        calls.searched.append(query)
        body = " ".join(f"finding{n}for{len(calls.searched)}" for n in range(body_words))
        return ToolResult(tool_name="web_search", content=f"result {len(calls.searched)}: {body}")

    @tool(name="file_read", description="Read a file.")
    async def file_read(path: str) -> ToolResult:
        calls.read.append(path)
        return ToolResult(tool_name="file_read", content=f"contents {len(calls.read)}")

    @tool(name="file_write", description="Write a file.")
    async def file_write(path: str, content: str) -> ToolResult:
        calls.wrote.append(path)
        return ToolResult(tool_name="file_write", content=f"wrote {content} to {path}")

    return [web_search, file_read, file_write]


@tool(name="flaky", description="A tool that always reports the same failure.")
async def _flaky(query: str) -> ToolResult:
    _FLAKY_CALLS.append(query)
    return ToolResult(tool_name="flaky", content="upstream 503", is_error=True)


_FLAKY_CALLS: list[str] = []


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


def _batch(queries: list[str], prefix: str) -> ChatResponse:
    """One step asking for several lookups at once, which is what T11 tells it to do."""
    return _resp(
        tool_calls=[
            ToolCall(name="web_search", args={"query": q}, call_id=f"{prefix}{i}")
            for i, q in enumerate(queries)
        ]
    )


def _make_loop(
    script: list[ChatResponse],
    *,
    tools: list[object],
    ceiling_tokens: int | None = None,
    head_chars: int = 400,
    native_tools: bool = False,
) -> tuple[AgenticLoop, ScriptedBackend]:
    stores: dict[str, FakeStore] = {
        "identity": FakeStore(),
        "self_facts": FakeStore(),
        "worldview": FakeStore(),
        "episodic": FakeStore(),
    }
    backend = ScriptedBackend([], chat_script=script, supports_native_tools=native_tools)
    # The window the compactor keys off. Without it the fake backend reports the 4096
    # default, the compactor fires first and these tests would measure the wrong guard.
    backend.max_tokens = _REAL_WINDOW  # type: ignore[attr-defined]
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]
    pruner = (
        None
        if ceiling_tokens is None
        else ToolResultPruner(ceiling_tokens=ceiling_tokens, head_chars=head_chars)
    )
    loop = AgenticLoop(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(name="Astrid", role="researcher", background="bg"),
        ),
        stores=stores,  # type: ignore[arg-type]
        toolbox=Toolbox(list(tools), allow_list=None),  # type: ignore[arg-type]
        skill_injector=SkillInjector(),
        scanned_skills=[],
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        pruner=pruner,
    )
    return loop, backend


async def _run(
    loop: AgenticLoop, task: str = "research the deposit rules"
) -> tuple[Run, list[RunEvent]]:
    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    run = await loop.run(task, on_event=on_event)
    return run, events


def _guards(events: list[RunEvent]) -> list[str]:
    return [str(e.data["guard"]) for e in events if e.type == "call_skipped"]


# ----- (a) a repeat of a failed call is not dispatched ----------------------


@pytest.mark.asyncio
async def test_a_call_that_already_failed_is_not_dispatched_again() -> None:
    _FLAKY_CALLS.clear()
    same = {"query": "husleieloven deposit"}
    script = [
        _resp(tool_calls=[ToolCall(name="flaky", args=same, call_id="c1")]),
        _resp(tool_calls=[ToolCall(name="flaky", args=same, call_id="c2")]),
        _resp("[FINAL] I worked with what I had."),
    ]
    loop, _ = _make_loop(script, tools=[_flaky])

    run, events = await _run(loop)

    assert _FLAKY_CALLS == ["husleieloven deposit"]  # dispatched exactly once
    second = run.steps[1].results[0]
    assert second.is_error is True
    assert "upstream 503" in second.content  # the original error, not a new one
    assert REPEAT_ERROR_HINT in second.content  # plus what to do instead
    assert _guards(events) == ["repeat_error"]


@pytest.mark.asyncio
async def test_a_different_query_after_a_failure_is_still_dispatched() -> None:
    """The guard refuses the REPEAT, never the retry. A model that takes the hint and
    changes its query must reach the tool."""
    _FLAKY_CALLS.clear()
    script = [
        _resp(tool_calls=[ToolCall(name="flaky", args={"query": "one"}, call_id="c1")]),
        _resp(tool_calls=[ToolCall(name="flaky", args={"query": "two"}, call_id="c2")]),
        _resp("[FINAL] done"),
    ]
    loop, _ = _make_loop(script, tools=[_flaky])

    _run_result, events = await _run(loop)

    assert _FLAKY_CALLS == ["one", "two"]
    assert _guards(events) == []


# ----- (b) a repeated read is served from the ledger ------------------------


@pytest.mark.asyncio
async def test_a_repeated_search_is_answered_from_the_ledger_at_zero_cost() -> None:
    calls = _Calls()
    script = [
        _search("deposit rules", "c1"),
        _search("deposit rules", "c2"),
        _resp("[FINAL] Here is what I found."),
    ]
    loop, _ = _make_loop(script, tools=_tools(calls))

    run, events = await _run(loop)

    assert calls.searched == ["deposit rules"]  # the second search never ran
    second = run.steps[1].results[0]
    assert second.content.startswith("result 1:")  # the stored answer, not a new one
    assert CACHED_RESULT_NOTE in second.content  # and it is not passed off as fresh
    assert second.is_error is False
    assert _guards(events) == ["cached_read"]


@pytest.mark.asyncio
async def test_a_write_between_two_identical_reads_yields_fresh_content() -> None:
    """D-W1-11's named condition. The ledger cannot know what a write touched, so any
    write invalidates every cached read: one re-read is cheap, a stale file is not."""
    calls = _Calls()
    read = ToolCall(name="file_read", args={"path": "/notes.md"}, call_id="c1")
    script = [
        _resp(tool_calls=[read]),
        _resp(
            tool_calls=[
                ToolCall(
                    name="file_write", args={"path": "/notes.md", "content": "x"}, call_id="c2"
                )
            ]
        ),
        _resp(tool_calls=[ToolCall(name="file_read", args={"path": "/notes.md"}, call_id="c3")]),
        _resp("[FINAL] done"),
    ]
    loop, _ = _make_loop(script, tools=_tools(calls))

    run, events = await _run(loop)

    assert calls.read == ["/notes.md", "/notes.md"]  # the read happened again
    assert run.steps[2].results[0].content == "contents 2"  # and it is the fresh content
    assert _guards(events) == []


@pytest.mark.asyncio
async def test_a_repeated_write_is_dispatched_every_time() -> None:
    """A successful side effect is never replayed: serving one from the ledger would
    report an effect that did not happen."""
    calls = _Calls()
    args = {"path": "/notes.md", "content": "x"}
    script = [
        _resp(tool_calls=[ToolCall(name="file_write", args=args, call_id="c1")]),
        _resp(tool_calls=[ToolCall(name="file_write", args=args, call_id="c2")]),
        _resp("[FINAL] done"),
    ]
    loop, _ = _make_loop(script, tools=_tools(calls))

    _run_result, events = await _run(loop)

    assert calls.wrote == ["/notes.md", "/notes.md"]
    assert _guards(events) == []


@pytest.mark.asyncio
async def test_the_ledger_does_not_outlive_its_run() -> None:
    """Nothing a previous run learned about the world is true now."""
    calls = _Calls()
    script = [_search("deposit rules", "c1"), _resp("[FINAL] done")]
    loop, backend = _make_loop(script, tools=_tools(calls))

    await _run(loop)
    backend._chat_index = 0  # replay the same script as a second run
    await _run(loop)

    assert calls.searched == ["deposit rules", "deposit rules"]


# ----- (c) the cost ceiling keeps a long run's steps flat -------------------


def _step_sizes(backend: ScriptedBackend) -> list[int]:
    """What each step actually sent, in tokens: the number the bill is made of."""
    return [ToolResultPruner.size(context) for context in backend.chat_contexts]


def _growth(sizes: list[int]) -> list[int]:
    """What each of the last steps added to the bill: a plateau reads as small numbers."""
    late = sizes[-3:]
    return [later - earlier for earlier, later in zip(late, late[1:], strict=False)]


_SIX_SEARCHES_THEN_FINAL = [
    *[_search("deposit rules", f"c{n}") for n in range(6)],
    _resp("[FINAL] Here is what I found."),
]


@pytest.mark.asyncio
async def test_six_repeated_searches_do_not_grow_the_bill() -> None:
    """The headline. A model stuck on one query used to dispatch six searches and carry six
    copies of the answer, each step costing one more copy than the last. It now searches
    once, and the carried copies stop compounding once the run crosses its cost ceiling.

    The control run in the same test is the same script with pruning off: it shows the
    climb the ruling described, so the plateau is measured against it rather than against a
    number someone picked.
    """
    guarded_calls, control_calls = _Calls(), _Calls()
    loop, backend = _make_loop(
        _SIX_SEARCHES_THEN_FINAL,
        tools=_tools(guarded_calls, body_words=300),
        ceiling_tokens=2_000,
        head_chars=200,
    )
    control, control_backend = _make_loop(
        _SIX_SEARCHES_THEN_FINAL,
        tools=_tools(control_calls, body_words=300),
        ceiling_tokens=0,
    )

    run, events = await _run(loop)
    await _run(control)

    assert guarded_calls.searched == ["deposit rules"]  # one dispatch for six asks
    one_result = count_tokens(run.steps[0].results[0].content)
    assert one_result > 1_000  # each answer is genuinely expensive to carry

    assert max(_growth(_step_sizes(control_backend))) > one_result  # the climb
    assert max(_growth(_step_sizes(backend))) < one_result // 4  # the plateau
    # Already a whole answer cheaper by the seventh step, and the gap widens with every
    # step after it: the control pays another full copy each time, the guarded run a head.
    assert _step_sizes(backend)[-1] < _step_sizes(control_backend)[-1] - one_result
    assert any(e.type == "context_pruned" for e in events)
    assert run.status.value == "completed"


@pytest.mark.asyncio
async def test_six_distinct_searches_are_trimmed_to_heads_with_a_marker() -> None:
    """Distinct results cannot be served from the ledger: each one is real new evidence.
    The cost guard is what holds the line, and it says what it cut."""
    calls = _Calls()
    script = [
        *[_search(f"query {n}", f"c{n}") for n in range(6)],
        _resp("[FINAL] Here is what I found."),
    ]
    loop, backend = _make_loop(
        script, tools=_tools(calls, body_words=300), ceiling_tokens=2_000, head_chars=200
    )

    _run_result, events = await _run(loop)

    assert len(calls.searched) == 6  # every distinct search really ran
    last_context = backend.chat_contexts[-1]
    trimmed = [m for m in last_context if PRUNED_MARKER in str(m.content)]
    assert trimmed, "old results should carry the marker saying what was cut"
    assert "result 1:" in str(trimmed[0].content)  # what it WAS is still readable
    assert PRUNED_MARKER not in str(last_context[-1].content)  # the newest result is intact
    assert last_context[0] is backend.chat_contexts[0][0]  # the floor is never touched
    pruned_events = [e for e in events if e.type == "context_pruned"]
    assert pruned_events
    assert pruned_events[0].data["after_tokens"] < pruned_events[0].data["before_tokens"]


@pytest.mark.asyncio
async def test_a_zero_ceiling_leaves_every_result_whole() -> None:
    """The rollback D-W1-13 promised: one env var to 0 and nothing is trimmed."""
    calls = _Calls()
    script = [
        *[_search(f"query {n}", f"c{n}") for n in range(6)],
        _resp("[FINAL] done"),
    ]
    loop, backend = _make_loop(script, tools=_tools(calls, body_words=300), ceiling_tokens=0)

    _run_result, events = await _run(loop)

    last_context = backend.chat_contexts[-1]
    assert not [m for m in last_context if PRUNED_MARKER in str(m.content)]
    assert not [e for e in events if e.type == "context_pruned"]
    sizes = _step_sizes(backend)
    assert sizes[-1] > sizes[2]  # unguarded, the climb is exactly what it always was


@pytest.mark.asyncio
async def test_the_native_tool_result_shape_is_trimmed_too() -> None:
    """A provider with native tool calling carries results as ``role="tool"``; one without
    carries the same text as a user turn (R9-068). Both have to be reachable, or half the
    providers keep paying full price."""
    calls = _Calls()
    script = [
        *[_search(f"query {n}", f"c{n}") for n in range(6)],
        _resp("[FINAL] done"),
    ]
    loop, backend = _make_loop(
        script,
        tools=_tools(calls, body_words=300),
        ceiling_tokens=2_000,
        head_chars=200,
        native_tools=True,
    )

    await _run(loop)

    last_context = backend.chat_contexts[-1]
    trimmed = [m for m in last_context if PRUNED_MARKER in str(m.content)]
    assert trimmed
    assert all(m.role == "tool" for m in trimmed)


# ----- (d) a batched step is never trimmed before the model has read it ----


_FIVE = ["rent", "deposit", "notice", "damage", "interest"]


@pytest.mark.asyncio
async def test_a_batched_step_reaches_the_model_whole(caplog: pytest.LogCaptureFixture) -> None:
    """D-W1-41. A step is one model call and every lookup it asked for, so a step that
    batches five searches is five or six messages. Protecting a fixed number of MESSAGES
    cut the oldest results of that step at the step boundary, before the model had ever
    been sent them, which is exactly what D-W1-13 refused when it rejected truncating at
    dispatch: the model needs a result whole on the step it arrives.
    """
    del caplog
    calls = _Calls()
    script = [
        _batch(_FIVE, "a"),  # step 0: five lookups at once
        _resp("[FINAL] Here is what I found."),
    ]
    loop, backend = _make_loop(
        script, tools=_tools(calls, body_words=300), ceiling_tokens=2_000, head_chars=200
    )

    run, _events = await _run(loop)

    assert calls.searched == _FIVE  # all five really ran
    sent_next = backend.chat_contexts[1]  # what the model was given on the next call
    results = [m for m in sent_next if "tool_name" in m.metadata]
    assert len(results) == 5
    for index, message in enumerate(results):
        assert PRUNED_MARKER not in str(message.content), f"result {index} was cut unread"
    assert run.status.value == "completed"


@pytest.mark.asyncio
async def test_the_native_shape_of_a_batched_step_reaches_the_model_whole() -> None:
    """The same claim for a provider with native tool calling, where the step is the
    assistant's tool_calls message plus five results."""
    calls = _Calls()
    script = [_batch(_FIVE, "a"), _resp("[FINAL] done")]
    loop, backend = _make_loop(
        script,
        tools=_tools(calls, body_words=300),
        ceiling_tokens=2_000,
        head_chars=200,
        native_tools=True,
    )

    await _run(loop)

    sent_next = backend.chat_contexts[1]
    results = [m for m in sent_next if m.role == "tool"]
    assert len(results) == 5
    assert not [m for m in results if PRUNED_MARKER in str(m.content)]


@pytest.mark.asyncio
async def test_a_batched_step_is_trimmed_once_two_further_steps_have_run() -> None:
    """The other half of the rule: protection is two STEPS, not forever. Once the model has
    been sent a step's results and moved two steps past them, they are as prunable as any
    other old output, or the guard would be an excuse never to prune at all."""
    calls = _Calls()
    script = [
        _batch(_FIVE, "a"),
        _search("later one", "b"),
        _search("later two", "c"),
        _resp("[FINAL] done"),
    ]
    loop, backend = _make_loop(
        script, tools=_tools(calls, body_words=300), ceiling_tokens=2_000, head_chars=200
    )

    _run_result, events = await _run(loop)

    final_context = backend.chat_contexts[-1]
    trimmed = [m for m in final_context if PRUNED_MARKER in str(m.content)]
    assert len(trimmed) == 5  # the whole batched step, and only it
    assert PRUNED_MARKER not in str(final_context[-1].content)  # the newest is intact
    assert any(e.type == "context_pruned" for e in events)


# ----- (d) the guard outcome survives the run (R9-157) ----------------------
#
# The disclosure used to exist only while someone was watching: the guards emitted
# ``call_skipped`` / ``context_pruned`` run events, the web reduced them into the step
# trace, and the RUN that gets persisted carried nothing. Nobody watches a task leg live,
# so the common case was a correct guard reading as a missing feature. These tests assert
# the durable half: what the guards did is ON the step, and it is on the step in the exact
# JSON ``persist_final`` writes to ``runs.steps``.


def _notes_json(run: Run, index: int) -> list[dict[str, object]]:
    """A step's notes exactly as the api persists them (``Step.model_dump(mode="json")``)."""
    dumped = run.steps[index].model_dump(mode="json")
    return list(dumped["notes"])


@pytest.mark.asyncio
async def test_a_cached_read_is_recorded_on_the_step_not_only_on_the_stream() -> None:
    calls = _Calls()
    script = [
        _search("deposit rules", "c1"),
        _search("deposit rules", "c2"),
        _resp("[FINAL] Here is what I found."),
    ]
    loop, _ = _make_loop(script, tools=_tools(calls))

    run, events = await _run(loop)

    assert _guards(events) == ["cached_read"]  # the live seam still says it
    assert _notes_json(run, 1) == [
        {"kind": "call_skipped", "tool": "web_search", "guard": "cached_read"}
    ]
    assert _notes_json(run, 0) == []  # the step that really searched claims nothing


@pytest.mark.asyncio
async def test_a_refused_repeat_records_which_guard_refused_it() -> None:
    """``repeat_error`` and ``cached_read`` are different events for the reader: one is a
    call that was refused, the other an answer served. The record has to say which."""
    _FLAKY_CALLS.clear()
    same = {"query": "husleieloven deposit"}
    script = [
        _resp(tool_calls=[ToolCall(name="flaky", args=same, call_id="c1")]),
        _resp(tool_calls=[ToolCall(name="flaky", args=same, call_id="c2")]),
        _resp("[FINAL] I worked with what I had."),
    ]
    loop, _ = _make_loop(script, tools=[_flaky])

    run, _events = await _run(loop)

    assert _notes_json(run, 1) == [
        {"kind": "call_skipped", "tool": "flaky", "guard": "repeat_error"}
    ]


@pytest.mark.asyncio
async def test_a_step_that_batched_two_skips_records_both() -> None:
    calls = _Calls()
    read = ToolCall(name="file_read", args={"path": "/notes.md"}, call_id="r1")
    search = ToolCall(name="web_search", args={"query": "deposit rules"}, call_id="s1")
    script = [
        _resp(tool_calls=[read, search]),
        _resp(
            tool_calls=[
                ToolCall(name="file_read", args={"path": "/notes.md"}, call_id="r2"),
                ToolCall(name="web_search", args={"query": "deposit rules"}, call_id="s2"),
            ]
        ),
        _resp("[FINAL] done"),
    ]
    loop, _ = _make_loop(script, tools=_tools(calls))

    run, _events = await _run(loop)

    assert [n["tool"] for n in _notes_json(run, 1)] == ["file_read", "web_search"]


@pytest.mark.asyncio
async def test_a_trim_is_recorded_on_the_step_it_happened_on() -> None:
    """The pruner fires at a step's boundary, after the step is already built. The note has
    to land on THAT step, the same one the live ``context_pruned`` event names, or a
    reopened run would attribute the trim to the wrong place in the timeline."""
    calls = _Calls()
    script = [
        *[_search(f"query {n}", f"c{n}") for n in range(6)],
        _resp("[FINAL] Here is what I found."),
    ]
    loop, _ = _make_loop(
        script, tools=_tools(calls, body_words=300), ceiling_tokens=2_000, head_chars=200
    )

    run, events = await _run(loop)

    pruned_events = [e for e in events if e.type == "context_pruned"]
    assert pruned_events, "the run must actually have crossed the ceiling"
    live_steps = [e.step for e in pruned_events]
    recorded_steps = [
        i for i, s in enumerate(run.steps) if any(n.kind == "context_pruned" for n in s.notes)
    ]
    assert recorded_steps == live_steps  # the durable story matches the watched one

    first = next(n for n in _notes_json(run, live_steps[0]) if n["kind"] == "context_pruned")
    assert int(str(first["after_tokens"])) < int(str(first["before_tokens"]))  # the saving


@pytest.mark.asyncio
async def test_a_run_whose_guards_never_fired_records_no_notes() -> None:
    """The contrast. Notes are what the guards DID, so a run that repeated nothing and
    never crossed the ceiling must carry none: an empty list on every step, so a reopened
    run of honest work shows a clean trace rather than a manufactured one."""
    calls = _Calls()
    script = [
        _search("deposit rules", "c1"),
        _search("notice periods", "c2"),
        _resp("[FINAL] done"),
    ]
    loop, _ = _make_loop(script, tools=_tools(calls))

    run, events = await _run(loop)

    assert len(calls.searched) == 2  # both searches really ran
    assert _guards(events) == []
    assert all(s.notes == [] for s in run.steps)
    assert all(step.model_dump(mode="json")["notes"] == [] for step in run.steps)


def test_a_step_persisted_before_notes_existed_still_loads() -> None:
    """Backward compatibility: every run already in the database was written without this
    field. Loading one must give a step with no guard activity, not an error."""
    legacy = {
        "type": "tool_call",
        "tool_calls": [{"name": "web_search", "args": {"query": "x"}, "call_id": "c1"}],
        "results": [{"tool_name": "web_search", "content": "hit", "call_id": "c1"}],
        "tier_used": "small",
        "tokens": 15,
        "latency_ms": 1.0,
    }

    step = Step.model_validate(legacy)

    assert step.notes == []
