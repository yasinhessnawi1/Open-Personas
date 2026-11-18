"""``record_user_fact`` carries its interaction and records the disclosure fact (R9-253).

A direct write used to carry no interaction id, so a disclosure the persona recorded never
gated its conversation, and its untagged siblings reached initiative's scan. The write now
carries the interaction its turn is bound to (the api binds the scope per turn, run and leg),
and a tagged or means-rejected write records the durable disclosure fact BEFORE anything is
written. A fact that cannot be recorded refuses the write: nothing tagged lands without it.
"""

# ruff: noqa: ARG002 - the doubles ignore some args by design.

from __future__ import annotations

import pytest
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome
from persona_runtime.extraction.direct_write import make_record_user_fact_tool
from persona_runtime.interaction_scope import (
    InteractionRef,
    bind_interaction,
    current_interaction,
    reset_interaction,
)

_OWNER = "u1"
_PERSONA = "assistant"
_TURN = InteractionRef(kind="conversation", id="conv-7")


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []
        self.merged: list[KnowledgeCandidate] = []


class _Store:
    def __init__(self, log: _Log) -> None:
        self._log = log

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self._log.events.append(("merge",))
        self._log.merged.append(candidate)
        return MergeOutcome(action=MergeAction.CREATED, node_id="u1::node::00000001")


class _Facts:
    def __init__(self, log: _Log, *, held: bool = True, error: Exception | None = None) -> None:
        self._log = log
        self._held = held
        self._error = error

    def record(self, owner_id: str, persona_id: str, interaction_id: str) -> bool:
        self._log.events.append(("fact", owner_id, persona_id, interaction_id))
        if self._error is not None:
            raise self._error
        return self._held


def _tool(log: _Log, facts: _Facts | None, interaction: InteractionRef | None = _TURN) -> object:
    return make_record_user_fact_tool(
        graph_store=_Store(log),  # type: ignore[arg-type]
        owner_provider=lambda: _OWNER,
        persona_id=_PERSONA,
        interaction_provider=lambda: interaction,
        disclosures=facts,
    )


_FACT = ("fact", _OWNER, _PERSONA, "conv-7")


@pytest.mark.asyncio
async def test_the_write_carries_the_interaction_its_turn_is_bound_to() -> None:
    log = _Log()
    res = await _tool(log, _Facts(log)).execute(fact="I'm allergic to shellfish")  # type: ignore[attr-defined]
    assert not res.is_error
    provenance = log.merged[0].provenance
    assert (provenance.interaction_id, provenance.interaction_kind) == ("conv-7", "conversation")


@pytest.mark.asyncio
async def test_a_tagged_write_records_the_fact_before_the_merge() -> None:
    log = _Log()
    res = await _tool(log, _Facts(log)).execute(  # type: ignore[attr-defined]
        fact="I've been struggling with disordered eating", category="disordered_eating"
    )
    assert not res.is_error
    assert log.events == [_FACT, ("merge",)]


@pytest.mark.asyncio
async def test_an_untagged_write_records_no_fact() -> None:
    log = _Log()
    await _tool(log, _Facts(log)).execute(fact="I'm allergic to shellfish")  # type: ignore[attr-defined]
    assert log.events == [("merge",)]


@pytest.mark.parametrize(
    ("fact", "category"),
    [
        ("I keep thinking about taking all my pills at once", "self_harm"),
        ("I keep a noose in the shed", None),
    ],
    ids=["tagged", "untagged"],
)
@pytest.mark.asyncio
async def test_a_write_rejected_for_its_means_still_records_the_fact(
    fact: str, category: str | None
) -> None:
    log = _Log()
    res = await _tool(log, _Facts(log)).execute(fact=fact, category=category)  # type: ignore[attr-defined]
    assert res.is_error
    assert log.events == [_FACT], "the disclosure is remembered; the means are never stored"


@pytest.mark.parametrize(
    "facts_kwargs", [{"error": OSError("database down")}, {"held": False}], ids=["fails", "gone"]
)
@pytest.mark.asyncio
async def test_a_fact_that_is_not_held_refuses_the_write(facts_kwargs: dict[str, object]) -> None:
    log = _Log()
    res = await _tool(log, _Facts(log, **facts_kwargs)).execute(  # type: ignore[arg-type, attr-defined]
        fact="I've been struggling with disordered eating", category="disordered_eating"
    )
    assert res.is_error
    assert "Not recorded" in res.content
    assert "disordered" not in res.content, "the refusal never echoes the disclosure"
    assert log.events == [_FACT]


@pytest.mark.asyncio
async def test_an_unbound_write_is_tagged_and_written_as_before_with_no_fact() -> None:
    log = _Log()
    res = await _tool(log, _Facts(log), interaction=None).execute(  # type: ignore[attr-defined]
        fact="I've been struggling with disordered eating", category="disordered_eating"
    )
    assert not res.is_error
    assert log.events == [("merge",)]
    assert log.merged[0].provenance.interaction_id is None
    assert log.merged[0].wellbeing_category == "disordered_eating"


@pytest.mark.asyncio
async def test_without_a_recorder_the_write_still_carries_its_interaction() -> None:
    log = _Log()
    await _tool(log, None).execute(fact="a fact", category="disordered_eating")  # type: ignore[attr-defined]
    assert log.events == [("merge",)]
    assert log.merged[0].provenance.interaction_id == "conv-7"


def test_the_scope_binds_and_resets() -> None:
    assert current_interaction() is None
    token = bind_interaction("agentic_run", "run-3")
    try:
        assert current_interaction() == InteractionRef(kind="agentic_run", id="run-3")
    finally:
        reset_interaction(token)
    assert current_interaction() is None


def test_the_scope_refuses_an_empty_id() -> None:
    with pytest.raises(ValueError, match="interaction"):
        bind_interaction("conversation", "")


class _RecordingLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, object]]] = []

    def warning(self, message: str, **fields: object) -> None:
        self.lines.append((message, fields))


@pytest.mark.asyncio
async def test_a_failed_fact_logs_its_cause_by_class_never_by_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from persona_runtime.extraction import direct_write  # noqa: PLC0415

    logger = _RecordingLogger()
    monkeypatch.setattr(direct_write, "_logger", logger)
    log = _Log()
    await _tool(log, _Facts(log, error=OSError("struggling with eating"))).execute(  # type: ignore[attr-defined]
        fact="I've been struggling with disordered eating", category="disordered_eating"
    )
    ((_message, fields),) = logger.lines
    assert fields["error_class"] == "OSError"
    assert "struggling" not in repr(logger.lines), "no exception message, no disclosure text"
