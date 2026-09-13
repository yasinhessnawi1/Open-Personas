"""The loop can stop ON a question instead of answering it itself (Spec W1, D-W1-34).

A run whose caller is not present has always proceeded with best judgment when the model
asks (`_NO_CALLBACK_REPLY`), which is right for an initiative or a scheduled nudge: nobody
is there. Inside a TASK LEG that is wrong. The user is reachable, just not synchronously, so
the honest move is to stop, park the task on the question, and continue when they answer.

What is pinned here:

- with ``park_on_question`` the loop stops at the question, the run ends ``AWAITING_USER``,
  the last step carries the question unanswered, and no ``output`` is invented;
- the reserve of the script is never reached (the model does not get to talk itself into an
  answer, which is exactly how a task used to finish by asking itself three times);
- without the flag the old behaviour is byte-identical (proceed, reach [FINAL], COMPLETED);
- a SUPPRESSED question (per-run cap, D-21-5; repeat, D-21-6) still proceeds even when the
  flag is on, so a persona cannot strand its own task by asking the same thing forever;
- a real ``user_respond`` still wins: an answer in hand beats parking;
- control markers never reach the run's user-visible output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _fakes import FakeStore, ScriptedBackend  # type: ignore[import-not-found]
from persona.backends import BackendConfig, ChatResponse
from persona.backends.types import TokenUsage
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolResult
from persona.skills import SkillInjector
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.run import RunStatus
from persona_runtime.agentic.step import StepType
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from persona_runtime.agentic.events import RunEvent

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


@tool(name="echo", description="Echo.")
async def _echo(message: str) -> ToolResult:
    return ToolResult(tool_name="echo", content=message, is_error=False)


def _persona(autonomy: str = "cautious") -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
        autonomy=autonomy,  # type: ignore[arg-type]
    )


def _resp(content: str = "") -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="claude-sonnet-4-6",
        provider="anthropic",
        latency_ms=1.0,
    )


def _make_loop(
    script: list[ChatResponse],
    *,
    park: bool = False,
    persona: Persona | None = None,
    max_steps: int = 20,
) -> AgenticLoop:
    stores: dict[str, FakeStore] = {
        "identity": FakeStore(),
        "self_facts": FakeStore(),
        "worldview": FakeStore(),
        "episodic": FakeStore(),
    }
    backend = ScriptedBackend([], chat_script=script)
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]  # noqa: SLF001
    return AgenticLoop(
        persona=persona or _persona(),
        stores=stores,  # type: ignore[arg-type]
        toolbox=Toolbox([_echo], allow_list=None),  # type: ignore[arg-type]
        skill_injector=SkillInjector(),
        scanned_skills=[],
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        max_steps=max_steps,
        park_on_question=park,
    )


async def _run(loop: AgenticLoop, task: str = "book it", **kw: object):  # noqa: ANN202
    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    run = await loop.run(task, on_event=on_event, **kw)  # type: ignore[arg-type]
    return run, events


_ASKS_THEN_ANSWERS_ITSELF = [
    _resp("[ASK_USER] Which dentist, and which day?"),
    _resp("[FINAL] I booked one at random."),
]


@pytest.mark.asyncio
async def test_the_loop_stops_on_the_question_and_invents_no_answer() -> None:
    loop = _make_loop(_ASKS_THEN_ANSWERS_ITSELF, park=True)
    run, events = await _run(loop)

    assert run.status is RunStatus.AWAITING_USER
    assert run.output is None  # a question is not a deliverable
    assert run.error is None  # nor is it a failure
    last = run.steps[-1]
    assert last.type is StepType.ASK_USER
    assert last.question == "Which dentist, and which day?"  # marker stripped
    assert last.user_answer is None
    # The reserve is never reached: the model does not get to answer its own question.
    assert not any(s.type is StepType.FINAL for s in run.steps)
    assert [e.type for e in events if e.type == "asking_user"]


@pytest.mark.asyncio
async def test_without_the_flag_the_old_behaviour_is_unchanged() -> None:
    loop = _make_loop(_ASKS_THEN_ANSWERS_ITSELF, park=False)
    run, _ = await _run(loop)

    assert run.status is RunStatus.COMPLETED
    assert run.output == "I booked one at random."
    assert [s.type for s in run.steps] == [StepType.ASK_USER, StepType.FINAL]


@pytest.mark.asyncio
async def test_an_answer_in_hand_beats_parking() -> None:
    loop = _make_loop(_ASKS_THEN_ANSWERS_ITSELF, park=True)

    async def respond(_q: str) -> str:
        return "The one on Storgata, Tuesday."

    run, _ = await _run(loop, user_respond=respond)
    assert run.status is RunStatus.COMPLETED
    assert run.steps[0].user_answer == "The one on Storgata, Tuesday."


@pytest.mark.asyncio
async def test_it_parks_on_the_question_that_was_actually_asked() -> None:
    # `decisive` caps the run at ONE question (D-21-5): the first is asked, so that is the
    # one the task waits on.
    script = [
        _resp("[ASK_USER] First question?"),
        _resp("[ASK_USER] A second, different question?"),
        _resp("[FINAL] done anyway"),
    ]
    loop = _make_loop(script, park=True, persona=_persona("decisive"))
    run, events = await _run(loop)

    assert run.status is RunStatus.AWAITING_USER
    assert run.steps[-1].question == "First question?"
    assert len([e for e in events if e.type == "asking_user"]) == 1


@pytest.mark.asyncio
async def test_a_question_the_cap_suppressed_proceeds_even_with_parking_on() -> None:
    """A suppressed question keeps its ruled behaviour: carry on with best judgment.

    Reaching that branch takes a run where the FIRST question is answered (so the loop does
    not park on it) and a second is then refused by the cap. Without an answerer the loop
    parks on the first and the suppressed branch is never exercised at all, which is how a
    mutation that parked on suppressed questions too once slipped through: a persona could
    then strand its own task by asking past its cap.
    """
    script = [
        _resp("[ASK_USER] First question?"),
        _resp("[ASK_USER] A second, different question?"),
        _resp("[FINAL] done anyway"),
    ]
    loop = _make_loop(script, park=True, persona=_persona("decisive"))

    async def respond(_q: str) -> str:
        return "Tuesday"

    run, events = await _run(loop, user_respond=respond)

    assert run.status is RunStatus.COMPLETED  # NOT parked on the suppressed second question
    assert run.output == "done anyway"
    assert len([e for e in events if e.type == "asking_user"]) == 1  # the cap held
    asked = [s for s in run.steps if s.type is StepType.ASK_USER]
    assert len(asked) == 2  # both consumed a step (D-21-15)
    assert asked[1].user_answer is None  # the suppressed one was never put to anyone


@pytest.mark.asyncio
async def test_a_repeated_question_proceeds_even_with_parking_on() -> None:
    """The dedup registry (D-21-6) suppresses a repeat, and a repeat must not re-park: the
    same words twice would otherwise stop the task again the moment it resumed."""
    script = [
        _resp("[ASK_USER] Which day?"),
        _resp("[ASK_USER] Which day?"),
        _resp("[FINAL] Tuesday it is."),
    ]
    loop = _make_loop(script, park=True)

    async def respond(_q: str) -> str:
        return "Tuesday"

    run, events = await _run(loop, user_respond=respond)
    assert run.status is RunStatus.COMPLETED
    assert len([e for e in events if e.type == "asking_user"]) == 1  # the repeat was refused


@pytest.mark.asyncio
async def test_control_markers_never_reach_the_runs_visible_output() -> None:
    """The leak this closes is the max-steps summary, not the final answer.

    A response carrying `[ASK_USER]` is classified as a question, so it never becomes a
    `[FINAL]`. What DID reach the user was the best-effort summary: it summarises a context
    full of assistant turns, markers and all, and that text became the run's output, the
    checkpoint's conclusions, and from there the review line and the task report.
    """
    script = [
        _resp("[ASK_USER] Which day?"),
        _resp("Best effort: I asked [ASK_USER] Which day? and got no answer. [FINAL]"),
    ]
    loop = _make_loop(script, park=False, max_steps=1)
    run, _ = await _run(loop)

    assert run.status is RunStatus.MAX_STEPS_REACHED
    assert run.output is not None
    assert "[ASK_USER]" not in run.output
    assert "[FINAL]" not in run.output
    assert "Which day?" in run.output  # the words survive; only the control markers go
