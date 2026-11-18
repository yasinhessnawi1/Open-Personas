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
