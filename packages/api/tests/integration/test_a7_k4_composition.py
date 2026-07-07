"""A7 criterion 8 — the K4-composition adversarial fixture (the A5 criterion-6 fixture, extended to
the event door).

*Gated-category content in an event never becomes an unprompted initiative subject via door (b).*
This drives the REGISTERED HANDLER path — the same :class:`EventCandidateHandler` the worker
registers — over a REAL graph under RLS:

- **The load-bearing proof:** a wellbeing-flagged node was sourced from a conversation (its
  provenance ``interaction_id`` is that conversation). An ``event_candidate`` job grounding on that
  conversation reaches the handler; layer (a) — the deterministic subject-exclusion at the seam —
  drops it BEFORE the producer runs. A hostile producer whose backend WOULD emit a well-formed
  candidate is never even consulted; nothing reaches the pipeline sink; the drop is audited
  (``event_trigger.candidate_wellbeing_dropped``), never silent.
- **The positive control:** a plain conversation (no flagged node sourced from it) passes the gate,
  the producer scores a candidate, and it reaches the sink — proving the gate excludes the sensitive
  subject specifically, not everything.

Real Postgres under the non-superuser ``persona_app`` role (RLS non-vacuous). Skips without
``APP_DATABASE_URL``.
"""

# ruff: noqa: ARG001 — the app_engine fixture depends on migrated_engine only for ordering
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.backends.types import ChatResponse, TokenUsage
from persona.graph import build_graph_store
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.initiative import InitiativeSettings
from persona.schema.chunks import WriteSource
from persona_api.events import ApiEventWellbeingCheck, SmallTierEventCandidateProducer
from persona_api.events.candidate_handler import EventCandidateHandler, EventCandidatePayload
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import audit_service
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
_OWNER = "own_a7k4"
_PERSONA = "pers_a7k4"
_GATED_CONV = "conv_gated_a7"  # a conversation a flagged node was sourced from
_PLAIN_CONV = "conv_plain_a7"  # a conversation with no flagged provenance
_GATED_NODE = f"{_OWNER}::node::gated_a7"
DIM = 384

_GOOD_CANDIDATE = """{"candidate": {
  "observation": "The landlord confirmed the inspection is this Friday.",
  "trigger": "approaching_commitment",
  "why_now": "The message just set a dated commitment two days out.",
  "plan": [{"description": "summarise the message", "categories": ["observe"]}],
  "next_step": "Summarise the inspection details for review.",
  "value": 0.8, "acceptance": 0.7, "urgency": "batch"}}"""


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


class _WouldEmitBackend:
    """A producer backend that WOULD surface a candidate — proving it is never reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:  # noqa: ARG002
        self.calls += 1
        return ChatResponse(
            content=_GOOD_CANDIDATE,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="small",
            provider="local",
            latency_ms=1.0,
        )


class _FakeGrounding:
    """The A5 grounding surface, stubbed — every conversation resolves to citable content."""

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None:  # noqa: ARG002
        return "Landlord: the inspection is this Friday at 10am."

    def task_content(self, owner_id: str, task_id: str) -> str | None:  # noqa: ARG002
        return None


class _CapturingSink:
    def __init__(self) -> None:
        self.submitted: list[tuple[object, ...]] = []

    async def submit(self, candidates: tuple[object, ...]) -> None:
        self.submitted.append(candidates)


def _gated_node() -> ConceptNode:
    """A wellbeing-flagged node whose provenance names the gated conversation as its source."""
    return ConceptNode(
        id=_GATED_NODE,
        node_kind=NodeKind.CIRCUMSTANCE,
        concept_name="gated_a7",
        content="The user disclosed self-harm urges in this thread.",
        wellbeing_category="self_harm",
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                persona_id=_PERSONA,
                interaction_id=_GATED_CONV,  # <-- the reverse-link layer (a) reads
                interaction_kind="conversation",
                written_at=_NOW,
            ),
        ),
        created_at=_NOW,
    )


def _seed(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": _PERSONA, "u": _OWNER},
        )
    backend = PostgresGraphBackend(engine=migrated_engine)
    backend.insert_node_if_absent(_OWNER, _gated_node(), [1.0] + [0.0] * (DIM - 1))


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _audit_count(engine: Engine, action: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM audit_log WHERE user_id = :u AND action = :a"),
            {"u": _OWNER, "a": action},
        ).scalar_one()


def _handler(
    app_engine: Engine, migrated_engine: Engine, backend: _WouldEmitBackend, sink: _CapturingSink
) -> EventCandidateHandler:
    """The handler exactly as the worker registers it — real wellbeing check, real audit sink."""
    graph_store = build_graph_store(
        engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger()
    )

    def _audit(
        owner: str, action: str, target: str, metadata: Mapping[str, str] | None = None
    ) -> None:
        audit_service.record(
            engine=migrated_engine,
            user_id=owner,
            action=action,
            target=target,
            metadata=dict(metadata) if metadata else None,
        )

    return EventCandidateHandler(
        producer=SmallTierEventCandidateProducer(
            backend=backend,  # type: ignore[arg-type]
            grounding=_FakeGrounding(),
            settings=InitiativeSettings(),
        ),
        sink=sink,
        wellbeing=ApiEventWellbeingCheck(graph_store),
        audit=_audit,
    )


def _payload(conversation_id: str) -> EventCandidatePayload:
    return EventCandidatePayload(
        persona_id=_PERSONA,
        event_kind="connector.message_received",
        event_id=f"evt-{conversation_id}",
        trigger_id="trg-a7k4",
        human="a message arrived",
        causal_chain=("trg-a7k4",),
        grounding_kind="conversation",
        grounding_ref=conversation_id,
    )


def test_gated_event_content_never_becomes_an_initiative_subject(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Criterion 8: a wellbeing-gated grounding is dropped at the seam, before the model."""
    _seed(migrated_engine)
    before = _audit_count(migrated_engine, "event_trigger.candidate_wellbeing_dropped")
    backend, sink = _WouldEmitBackend(), _CapturingSink()
    ctx = SimpleNamespace(owner_id=_OWNER)
    token = current_user_id.set(_OWNER)
    try:
        handler = _handler(app_engine, migrated_engine, backend, sink)
        asyncio.run(handler.handle(_payload(_GATED_CONV), ctx))  # type: ignore[arg-type]
    finally:
        current_user_id.reset(token)
    assert backend.calls == 0  # the producer/model is NEVER reached — the gate is at the seam
    assert sink.submitted == []  # nothing enters the pipeline
    assert (
        _audit_count(migrated_engine, "event_trigger.candidate_wellbeing_dropped") == before + 1
    )  # the drop is audited, never silent


def test_plain_event_content_passes_the_gate_to_the_pipeline(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The positive control: a NON-gated grounding flows through — the gate is specific, not all."""
    _seed(migrated_engine)
    backend, sink = _WouldEmitBackend(), _CapturingSink()
    ctx = SimpleNamespace(owner_id=_OWNER)
    token = current_user_id.set(_OWNER)
    try:
        handler = _handler(app_engine, migrated_engine, backend, sink)
        asyncio.run(handler.handle(_payload(_PLAIN_CONV), ctx))  # type: ignore[arg-type]
    finally:
        current_user_id.reset(token)
    assert backend.calls == 1  # the plain grounding passed the gate → the producer ran
    assert len(sink.submitted) == 1  # one candidate reached the pipeline sink
    (candidate,) = sink.submitted[0]
    assert candidate.citations[0].ref == _PLAIN_CONV  # grounded on the event's conversation
