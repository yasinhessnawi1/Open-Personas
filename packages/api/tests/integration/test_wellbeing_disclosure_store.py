"""R9-253 on real Postgres: the disclosure fact store, and the gate after a real forget.

As ``persona_app`` under row level security: a fact is idempotent, owner scoped, and refused
for a persona that no longer exists; and once the user deletes the tagged node through the
graph store, the untagged node learned beside it is still gated, by the fact alone.

Postgres only (CI). The fixtures skip when no database is configured.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph import build_graph_store
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.initiative import CitationKind, GroundingCitation
from persona.schema.chunks import WriteSource
from persona_api.initiative.disclosures import PgDisclosureStore
from persona_api.initiative.pipeline_wiring import ApiWellbeingSubjectCheck
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_OWNER = "own_r9253"
_OTHER = "own_r9253_other"
_PERSONA = "pers_r9253"
_TAGGED = f"{_OWNER}::node::00000001"
_DINNER = f"{_OWNER}::node::00000002"
_DIM = 384


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return _DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in texts]


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 (ordering)
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(super_engine: Engine) -> None:
    with super_engine.begin() as conn:
        for owner in (_OWNER, _OTHER):
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, :e)"),
                {"u": owner, "e": f"{owner}@example.test"},
            )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A')"),
            {"p": _PERSONA, "u": _OWNER},
        )
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES ('conv-d', :u, :p)"),
            {"u": _OWNER, "p": _PERSONA},
        )


def _as(owner: str, fn: Callable[[], Any]) -> Any:  # noqa: ANN401
    token = current_user_id.set(owner)
    try:
        return fn()
    finally:
        current_user_id.reset(token)


def test_a_fact_is_idempotent_and_its_owners_alone(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    facts = PgDisclosureStore(app_engine)
    assert _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d")) is True
    assert _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d")) is True
    assert _as(_OWNER, lambda: facts.disclosed(_OWNER, ["conv-d", "conv-x"])) == {"conv-d"}
    assert _as(_OTHER, lambda: facts.disclosed(_OTHER, ["conv-d"])) == set()
    with migrated_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM wellbeing_disclosures")).scalar_one()
    assert count == 1


def test_a_fact_is_refused_for_a_persona_that_is_gone_or_anothers(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    facts = PgDisclosureStore(app_engine)
    assert _as(_OWNER, lambda: facts.record(_OWNER, "pers_erased", "conv-d")) is False
    # Another owner naming this owner's persona: refused by the persona read and by RLS.
    assert _as(_OTHER, lambda: facts.record(_OTHER, _PERSONA, "conv-d")) is False
    with migrated_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM wellbeing_disclosures")).scalar_one()
    assert count == 0


def _node(node_id: str, wellbeing: str | None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"content of {node_id}",
        wellbeing_category=wellbeing,
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                persona_id=_PERSONA,
                interaction_id="conv-d",
                interaction_kind="conversation",
                written_at=_NOW,
            ),
        ),
        created_at=_NOW,
    )


def test_the_sibling_stays_gated_after_the_tagged_node_is_really_deleted(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=False, node_releaser=None)
    vec = [1.0] + [0.0] * (_DIM - 1)
    backend.insert_node_if_absent(_OWNER, _node(_TAGGED, "disordered_eating"), vec)
    backend.insert_node_if_absent(_OWNER, _node(_DINNER, None), vec)
    store = build_graph_store(
        engine=app_engine,
        embedder=_Emb(),
        audit_logger=MemoryAuditLogger(),
        persona_guard=False,
        node_releaser=None,
    )
    _as(_OWNER, lambda: PgDisclosureStore(app_engine).record(_OWNER, _PERSONA, "conv-d"))
    check = ApiWellbeingSubjectCheck(store, app_engine)
    dinner = (GroundingCitation(kind=CitationKind.NODE, ref=_DINNER),)

    assert _as(_OWNER, lambda: store.delete_node(_OWNER, _TAGGED)) is True
    assert _as(_OWNER, lambda: store.flagged_nodes(_OWNER)) == []

    gated = _as(_OWNER, lambda: check.tagged_refs(_OWNER, dinner, include_unsynthesised=False))
    assert gated == {_DINNER}


# D-ORCH-11-18-u: the release and the re-block on real Postgres, as persona_app.

_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _state(engine: Engine, owner: str = _OWNER) -> Any:  # noqa: ANN401
    return _as(owner, lambda: PgDisclosureStore(engine).states(owner, ["conv-d"]).get("conv-d"))


def _audit(super_engine: Engine) -> list[tuple[str, str, str, Any]]:
    with super_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT user_id, action, target, metadata FROM audit_log ORDER BY created_at")
        ).all()
    return [(str(r[0]), str(r[1]), str(r[2]), r[3]) for r in rows]


def test_a_release_covers_what_was_recorded_and_a_new_disclosure_holds_it_again(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    from persona_api.initiative.disclosures import ReleaseOutcome  # noqa: PLC0415

    _seed(migrated_engine)
    facts = PgDisclosureStore(app_engine)
    _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d"))
    _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d"))
    assert _state(app_engine).disclosure_seq == 2

    stale = _as(_OWNER, lambda: facts.release(_OWNER, _PERSONA, "conv-d", version=1, at=_AT))
    assert stale is ReleaseOutcome.CONFLICT
    assert _state(app_engine).released_seq is None

    done = _as(_OWNER, lambda: facts.release(_OWNER, _PERSONA, "conv-d", version=2, at=_AT))
    assert done is ReleaseOutcome.CHANGED
    assert _state(app_engine).release_in_force is True
    assert _state(app_engine).released_at == _AT

    _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d"))
    after = _state(app_engine)
    assert (after.disclosure_seq, after.released_seq) == (3, 2)
    assert after.release_in_force is False

    assert _as(_OWNER, lambda: facts.hold(_OWNER, _PERSONA, "conv-d")) is ReleaseOutcome.CHANGED
    assert _state(app_engine).released_seq is None
    assert _as(_OWNER, lambda: facts.hold(_OWNER, _PERSONA, "conv-d")) is ReleaseOutcome.UNCHANGED

    assert _audit(migrated_engine) == [
        (_OWNER, "conversation.setting_changed", "conv-d", {"persona_id": _PERSONA}),
        (_OWNER, "conversation.setting_changed", "conv-d", {"persona_id": _PERSONA}),
    ]


def test_a_conversation_held_by_a_tagged_node_alone_is_born_released(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    from persona_api.initiative.disclosures import ReleaseOutcome  # noqa: PLC0415

    _seed(migrated_engine)
    facts = PgDisclosureStore(app_engine)
    got = _as(_OWNER, lambda: facts.release(_OWNER, _PERSONA, "conv-d", version=0, at=_AT))
    assert got is ReleaseOutcome.CHANGED
    state = _state(app_engine)
    assert (state.disclosure_seq, state.released_seq) == (1, 1)
    assert state.release_in_force is True


def test_another_owner_cannot_release_or_see_the_release(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    from persona_api.initiative.disclosures import ReleaseOutcome  # noqa: PLC0415

    _seed(migrated_engine)
    facts = PgDisclosureStore(app_engine)
    _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d"))
    forged = _as(_OTHER, lambda: facts.release(_OTHER, _PERSONA, "conv-d", version=1, at=_AT))
    assert forged is ReleaseOutcome.GONE, "not their persona"
    assert _as(_OTHER, lambda: facts.hold(_OTHER, _PERSONA, "conv-d")) is ReleaseOutcome.UNCHANGED
    assert _state(app_engine, _OTHER) is None
    assert _state(app_engine).released_seq is None
    assert _audit(migrated_engine) == []


def test_a_release_for_a_persona_that_is_gone_writes_nothing(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    from persona_api.initiative.disclosures import ReleaseOutcome  # noqa: PLC0415

    _seed(migrated_engine)
    facts = PgDisclosureStore(app_engine)
    got = _as(_OWNER, lambda: facts.release(_OWNER, "pers_erased", "conv-d", version=0, at=_AT))
    assert got is ReleaseOutcome.GONE
    assert _state(app_engine) is None
    assert _audit(migrated_engine) == []


def test_a_release_lifts_the_sibling_but_never_the_conversation_or_the_tagged_node(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """D-ORCH-11-18-u end to end, as persona_app: the node half and the fact half both lift."""
    _seed(migrated_engine)
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=False, node_releaser=None)
    vec = [1.0] + [0.0] * (_DIM - 1)
    backend.insert_node_if_absent(_OWNER, _node(_TAGGED, "disordered_eating"), vec)
    backend.insert_node_if_absent(_OWNER, _node(_DINNER, None), vec)
    store = build_graph_store(
        engine=app_engine,
        embedder=_Emb(),
        audit_logger=MemoryAuditLogger(),
        persona_guard=False,
        node_releaser=None,
    )
    facts = PgDisclosureStore(app_engine)
    _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d"))
    _as(_OWNER, lambda: facts.release(_OWNER, _PERSONA, "conv-d", version=1, at=_AT))
    check = ApiWellbeingSubjectCheck(store, app_engine)
    cited = (
        GroundingCitation(kind=CitationKind.NODE, ref=_DINNER),
        GroundingCitation(kind=CitationKind.NODE, ref=_TAGGED),
        GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-d"),
    )
    gated = _as(_OWNER, lambda: check.tagged_refs(_OWNER, cited, include_unsynthesised=True))
    assert gated == {_TAGGED, "conv-d"}

    _as(_OWNER, lambda: facts.record(_OWNER, _PERSONA, "conv-d"))
    again = _as(_OWNER, lambda: check.tagged_refs(_OWNER, cited, include_unsynthesised=True))
    assert again == {_DINNER, _TAGGED, "conv-d"}, "a disclosure after the release holds it again"


def test_the_control_reads_and_changes_only_the_owners_conversation(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    from persona_api.errors import ConversationNotFoundError  # noqa: PLC0415
    from persona_api.initiative.conversation_hold import ConversationHoldService  # noqa: PLC0415

    _seed(migrated_engine)
    store = build_graph_store(
        engine=app_engine,
        embedder=_Emb(),
        audit_logger=MemoryAuditLogger(),
        persona_guard=False,
        node_releaser=None,
    )
    service = ConversationHoldService(store, app_engine)
    assert _as(_OWNER, lambda: service.read(_OWNER, "conv-d")).state == "not_held"
    _as(_OWNER, lambda: PgDisclosureStore(app_engine).record(_OWNER, _PERSONA, "conv-d"))
    held = _as(_OWNER, lambda: service.read(_OWNER, "conv-d"))
    assert (held.state, held.version) == ("held", 1)

    with pytest.raises(ConversationNotFoundError):
        _as(_OTHER, lambda: service.read(_OTHER, "conv-d"))
    with pytest.raises(ConversationNotFoundError):
        _as(
            _OTHER,
            lambda: service.change(_OTHER, "conv-d", released=True, version=1, at=_AT),
        )

    done = _as(_OWNER, lambda: service.change(_OWNER, "conv-d", released=True, version=1, at=_AT))
    assert (done.state, done.released_at) == ("released", _AT)
    undone = _as(
        _OWNER, lambda: service.change(_OWNER, "conv-d", released=False, version=None, at=_AT)
    )
    assert undone.state == "held"
