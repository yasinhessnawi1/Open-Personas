"""A failed episodic forget is finished by its job, or retried by the user (R9-311), on Postgres.

``chat_service.delete_conversation`` with ``forget_memory`` and a durable plan records the forget
in the row delete's own transaction: the ids of the chunks stamped with the conversation, the
conversation marked forgotten, and the ``conversation_memory_forget`` job. When the forget the
request runs at once fails, the conversation is gone but the job holds every id, and running it
removes them. A write still in flight is refused by the guard (migration 068), so nothing comes
back after the job. Without a durable plan the forget runs first and a failure keeps the row.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import MemoryOwnerMissingError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.episodic import EpisodicStore
from persona.stores.postgres import PostgresBackend
from persona_api.config import APIConfig
from persona_api.db.audit_factory import build_audit_logger
from persona_api.jobs.context import WorkerJobContext
from persona_api.jobs.handlers.conversation_memory_forget import ConversationMemoryForgetHandler
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import chat_service, conversation_memory_forget
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona.audit import AuditLogger
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_OWNER = "u_r9_311"
_PERSONA = "persona_31131131131131131131131131131131"
_CONV = "conv_r9_311"
_OTHER = "conv_r9_311_other"
_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class _FailingStore:
    """The real store, except that the forget fails: the request's own attempt is lost."""

    def __init__(self, store: EpisodicStore) -> None:
        self._store = store

    def get_all(self, persona_id: str) -> list[PersonaChunk]:
        return self._store.get_all(persona_id)

    def remove_documents(self, persona_id: str, doc_ids: list[str]) -> None:  # noqa: ARG002
        msg = "the memory store is unavailable"
        raise RuntimeError(msg)


@pytest.fixture
def rls(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping the R9-311 forget test")
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'r9311@x.test')"), {"o": _OWNER}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
        for conversation in (_CONV, _OTHER):
            conn.execute(
                text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
                {"c": conversation, "o": _OWNER, "p": _PERSONA},
            )
    engine = make_rls_engine(app_url)
    token = current_user_id.set(_OWNER)
    try:
        yield engine
    finally:
        current_user_id.reset(token)
        engine.dispose()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return build_audit_logger(APIConfig(audit_root=str(tmp_path / "audit")), None)


def _write(backend: PostgresBackend, conversation_id: str) -> str:
    chunk_id = mint_chunk_id(_PERSONA, "episodic")
    backend.upsert(
        persona_id=_PERSONA,
        store_kind="episodic",
        chunks=[
            PersonaChunk(
                id=chunk_id,
                text=f"USER: hello {chunk_id}\nASSISTANT: hi",
                metadata={"importance": "0.5", "conversation_id": conversation_id},
                created_at=_NOW,
                provenance=ChunkProvenance(
                    source=WriteSource.SYSTEM,
                    logical_id=chunk_id,
                    version=1,
                    written_at=_NOW,
                    written_by="runtime.loop",
                ),
            )
        ],
    )
    return chunk_id


def _ids(backend: PostgresBackend) -> set[str]:
    return {c.id for c in backend.get_all(persona_id=_PERSONA, store_kind="episodic")}


def _delete(rls: Engine, store: object, *, durable: bool) -> None:
    chat_service.delete_conversation(
        rls_engine=rls,
        conversation_id=_CONV,
        forget_memory=True,
        persona_id=_PERSONA,
        episodic_store=store,  # type: ignore[arg-type]
        memory_plan=conversation_memory_forget.plan(
            owner_id=_OWNER, conversation_id=_CONV, durable=durable
        ),
    )


def test_a_forget_that_fails_after_the_delete_is_finished_by_its_job(
    rls: Engine, migrated_engine: Engine, embedder: HashEmbedder384, audit: AuditLogger
) -> None:
    backend = PostgresBackend(engine=rls, embedder=embedder)
    mine = {_write(backend, _CONV), _write(backend, _CONV)}
    theirs = _write(backend, _OTHER)
    store = EpisodicStore(backend=backend, audit_logger=audit)

    _delete(rls, _FailingStore(store), durable=True)

    with migrated_engine.connect() as conn:
        gone = conn.execute(
            text("SELECT count(*) FROM conversations WHERE id = :c"), {"c": _CONV}
        ).scalar()
        job = conn.execute(
            text("SELECT payload FROM jobs WHERE type = :t AND owner_id = :o"),
            {"t": conversation_memory_forget.JOB_TYPE, "o": _OWNER},
        ).scalar_one()
        marked = conn.execute(
            text("SELECT persona_id FROM forgotten_conversations WHERE conversation_id = :c"),
            {"c": _CONV},
        ).scalar_one()
    assert gone == 0
    assert marked == _PERSONA
    assert sorted(job["chunk_ids"]) == sorted(mine)
    assert _ids(backend) == {*mine, theirs}, "the request's forget failed: still there"

    # A turn still streaming writes after the delete: refused, so nothing comes back later.
    with pytest.raises(MemoryOwnerMissingError):
        _write(backend, _CONV)

    payload = conversation_memory_forget.ConversationMemoryForgetPayload.model_validate(job)
    context = WorkerJobContext(
        owner_id=_OWNER,
        rls_engine=rls,
        job_id="job-1",
        job_type=conversation_memory_forget.JOB_TYPE,
    )
    asyncio.run(
        ConversationMemoryForgetHandler(backend=backend, audit_logger=audit).handle(
            payload, context
        )
    )

    assert _ids(backend) == {theirs}, "the job forgot this conversation, and only it"


def test_without_a_job_a_failed_forget_keeps_the_conversation_for_a_retry(
    rls: Engine, migrated_engine: Engine, embedder: HashEmbedder384, audit: AuditLogger
) -> None:
    backend = PostgresBackend(engine=rls, embedder=embedder)
    mine = _write(backend, _CONV)
    store = EpisodicStore(backend=backend, audit_logger=audit)

    with pytest.raises(RuntimeError):
        _delete(rls, _FailingStore(store), durable=False)

    with migrated_engine.connect() as conn:
        kept = conn.execute(
            text("SELECT count(*) FROM conversations WHERE id = :c"), {"c": _CONV}
        ).scalar()
        marks = conn.execute(text("SELECT count(*) FROM forgotten_conversations")).scalar()
    assert kept == 1
    assert marks == 0
    assert mine in _ids(backend)

    _delete(rls, store, durable=False)

    assert mine not in _ids(backend)
    with pytest.raises(MemoryOwnerMissingError):
        _write(backend, _CONV)


def test_the_job_does_nothing_for_a_conversation_its_owner_did_not_forget(
    rls: Engine, embedder: HashEmbedder384, audit: AuditLogger
) -> None:
    backend = PostgresBackend(engine=rls, embedder=embedder)
    live = _write(backend, _OTHER)
    payload = conversation_memory_forget.ConversationMemoryForgetPayload(
        persona_id=_PERSONA, conversation_id=_OTHER, chunk_ids=[live]
    )
    context = WorkerJobContext(
        owner_id=_OWNER,
        rls_engine=rls,
        job_id="job-2",
        job_type=conversation_memory_forget.JOB_TYPE,
    )

    asyncio.run(
        ConversationMemoryForgetHandler(backend=backend, audit_logger=audit).handle(
            payload, context
        )
    )

    assert live in _ids(backend)
