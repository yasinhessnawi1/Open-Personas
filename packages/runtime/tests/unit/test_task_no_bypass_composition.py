"""Persona-ness + gated memory/graph, verified structurally (Spec A4, T11; criteria 5 & 6).

A4 re-asserts A2's integration properties at the persona level, with the discipline the voice
work taught (the ``project_voice_graph_unwired`` lesson): an autonomous path must never run a
*thinner* runtime or reach memory/the graph *around* the gates a conversation goes through. These
are structural checks — no live model, no DB — so a later fork of a task-specific runtime or a
bare graph read is caught in CI, not in production.

**V13 re-baseline (deliberate, not a deletion — the reviewed-re-baseline discipline).** The
``project_voice_graph_unwired`` state this file cites is *retired* by V13: voice no longer runs
graph-OFF-by-caution — it now reads the graph through the SAME K4-gated composition chat uses
(``build_voice_graph_retrieval`` mirrors ``_build_graph_retrieval``), off-loop and budgeted. The
PRINCIPLE is unchanged and now governs voice ACTIVELY, not by absence: *no path reads the graph
unguarded.* The voice-layer structural proof lives in the voice package
(``persona_voice/tests/unit/model/test_graph_no_bypass.py``: the turn context exposes the graph
only as the injected gated callable, never a raw store) — this file (persona-runtime) cannot
import persona-voice, so it documents the discipline; the leg assertion below still holds (a task
leg remains graph-OFF).

- **Criterion 5 (no-bypass):** a task leg drives the identical Spec-06 loop (``AgenticLoop``) via
  the ``AgenticRunner`` interface — there is no reduced/task-specific runner.
- **Criterion 6 (both directions, gated):**
  - *read:* the leg runtime cannot read the graph **bare** — ``AgenticLoop`` has no graph seam at
    all (graph-OFF, safe); chat's AND voice's (V13) read is the K4-gated ``graph_retrieval``
    (never raw). No path reads the graph unguarded.
  - *write (facts):* a task-learned fact routes through the K2 gate (``graph_store.merge``), never
    a bare insert.
  - *write (episodic):* task progress reaches episodic only through the milestone gate (restraint),
    not leg-by-leg.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome
from persona.tasks import TaskCheckpoint
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.legs.executor import AgenticRunner, LegDisposition, LegExecutor
from persona_runtime.legs.memory import TaskMilestone, milestone_for
from persona_runtime.loop import ConversationLoop

if TYPE_CHECKING:
    from collections.abc import Callable

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _params(func: Callable[..., object]) -> set[str]:
    return set(inspect.signature(func).parameters) - {"self"}


# --- Criterion 5: the leg drives the identical loop, no thinner runtime ----------------


def test_agentic_loop_satisfies_the_leg_runner_interface() -> None:
    # The leg drives an AgenticRunner; the unmodified AgenticLoop IS that runner — its run()
    # provides (a superset of) the leg contract's exact params (the leg calls task/on_event/
    # cancel_token; the loop's extra optional live-turn params don't change that). A thinner task
    # loop would not satisfy the full Spec-06 run interface.
    assert _params(AgenticRunner.run) <= _params(AgenticLoop.run)
    assert {"task", "on_event", "cancel_token"} <= _params(AgenticLoop.run)
    assert inspect.iscoroutinefunction(AgenticLoop.run)


def test_leg_executor_drives_the_full_loop_not_a_task_specific_one() -> None:
    # LegExecutor composes an AgenticRunner (the full Spec-06 loop) by dependency injection —
    # there is no LegLoop / TaskLoop reduced runtime it could substitute.
    ann = inspect.signature(LegExecutor.__init__).parameters["runner"].annotation
    assert ann in {AgenticRunner, "AgenticRunner"}


# --- Criterion 6 (read): no path reads the graph bare ----------------------------------


def test_agentic_loop_has_no_bare_graph_seam() -> None:
    # The leg runtime is AgenticLoop; it has NO graph_store / graph_retrieval parameter, so a leg
    # structurally cannot read the graph raw. K3 for legs is the flagged additive K4-gated seam
    # (A2-D-X-k3-seam) — until wired it is graph-OFF (safe), never bare.
    params = _params(AgenticLoop.__init__)
    assert not any("graph" in p for p in params), params


def test_chat_reads_the_graph_only_through_the_k4_gate() -> None:
    # The conversation loop's graph read is the injected K4-gated ``graph_retrieval`` — a callable,
    # not a raw graph store. So the ONE path that reads the graph does so through the gate.
    params = _params(ConversationLoop.__init__)
    assert "graph_retrieval" in params
    assert "graph_store" not in params  # never a raw store on the loop


# --- Criterion 6 (write, facts): the K2 gate, never a bare insert ----------------------


class _FakeGraphStore:
    """Records merge calls — the K2 gate. It offers no bare-insert method."""

    def __init__(self) -> None:
        self.merges: list[tuple[str, KnowledgeCandidate]] = []

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merges.append((owner_id, candidate))
        return MergeOutcome(action=MergeAction.CREATED, node_id="node-1")


@pytest.mark.asyncio
async def test_task_learned_fact_routes_through_the_k2_gate() -> None:
    from persona_runtime.extraction.direct_write import make_record_user_fact_tool

    store = _FakeGraphStore()
    tool = make_record_user_fact_tool(
        graph_store=store,  # type: ignore[arg-type]
        owner_provider=lambda: "user-1",
        persona_id="astrid",
    )
    result = await tool.execute(fact="the user prefers a window seat")
    assert not result.is_error
    # The write went through the gate (merge), owner-scoped — the only write path the tool has.
    assert len(store.merges) == 1
    owner, candidate = store.merges[0]
    assert owner == "user-1"
    assert candidate.content == "the user prefers a window seat"


# --- Criterion 6 (write, episodic): the milestone gate (restraint) ---------------------


def _cp(*, conclusions: tuple[str, ...]) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id="t1",
        leg_id="leg-1",
        checkpoint_seq=1,
        progress_conclusions=conclusions,
        updated_at=_NOW,
    )


def test_episodic_write_is_milestone_gated_not_per_leg() -> None:
    # The first leg is a milestone; a leg that establishes no new conclusion is NOT — so task
    # progress reaches episodic in digest shape, never leg-by-leg. (A2 owns the deep matrix in
    # test_leg_memory; this re-asserts the gate exists at the A4 integration surface.)
    started = milestone_for(
        is_first_leg=True,
        prior_checkpoint=None,
        new_checkpoint=_cp(conclusions=("started",)),
        disposition=LegDisposition.CONTINUE,
    )
    assert started is TaskMilestone.TASK_STARTED

    same = _cp(conclusions=("started",))
    no_progress = milestone_for(
        is_first_leg=False,
        prior_checkpoint=same,
        new_checkpoint=same,
        disposition=LegDisposition.CONTINUE,
    )
    assert no_progress is None  # no new conclusion → no episodic write (restraint)
