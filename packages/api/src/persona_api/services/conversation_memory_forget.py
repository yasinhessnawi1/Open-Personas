"""Forgetting a conversation's episodic memory, so a failure is retried, never stranded (R9-311).

``DELETE /v1/conversations/{id}?forget_memory=true`` used to delete the row and then forget the
episodic memory stamped with the conversation. When that forget failed the conversation was
already gone, the user could not ask again, and the chunks stayed for good.

The episodic store cannot join the row's transaction: it is its own engine (and Chroma on the
legacy edition), and a forget is several deletes (the raw chunks, then the gists and the core
block derived from them, K8-D-14). So the forget is made durable instead, the way the persona
erasure makes its memory step durable (R9-284): the ids are captured while they can still be
found, and the work is a retried, idempotent step that runs after the row is gone.

**With a worker that runs** :data:`JOB_TYPE`, inside the transaction that deletes the row
(:func:`record`, after the graph step): the conversation's row ``FOR UPDATE``; the ids of every
chunk stamped with it; the conversation marked forgotten (``forgotten_conversations``); and the
job, carrying those ids with the legacy ones content-matched before. They commit with the row
delete or not at all. The forget then runs at once, and if it fails the job retries it until
done. The ids are kept because a retry cannot find them again: once a run has removed the raw
chunks, the gists and the core block derived from them are reached only through those ids.

**Without one** (the legacy edition, a keyless boot): the forget runs before the row delete, so
a failure answers an error and leaves the conversation in place for the user to retry, as the
documents step does. The same in-transaction capture (on Postgres) then finds what was written
in between, removed after the commit.

**Late writes.** A turn still streaming, a voice call or an origination may write a stamped
chunk while the delete runs. The memory owner guard (migration 068) holds the conversation's row
``FOR KEY SHARE`` for every stamped write and refuses one once the row is gone and the
conversation is marked forgotten. The delete holds the row ``FOR UPDATE`` before it captures, so
a write either committed before the capture (and is captured) or waits for the delete and is
refused. What the job removes is therefore everything there will ever be. A plain delete marks
nothing, so its late writes are kept, as a plain delete keeps memory by contract.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from persona.jobs import JobPayload
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import forgotten_conversations as forgotten_t
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import memory_chunks as memory_chunks_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import users as users_t
from persona_api.jobs.queue import enqueue_on

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

__all__ = [
    "JOB_MAX_ATTEMPTS",
    "JOB_TYPE",
    "ConversationMemoryForgetPayload",
    "DurableForgetUnavailableError",
    "EpisodicForgetStore",
    "MemoryForgetPlan",
    "episodic_ids",
    "hold_owners",
    "idempotency_key",
    "is_forgotten",
    "job_attempt",
    "plan",
    "record",
    "remove",
]

JOB_TYPE = "conversation_memory_forget"

#: The job waits this long, so the forget the request runs at once usually leaves it nothing.
JOB_DELAY = timedelta(seconds=30)

#: With the backoff capped at an hour, about a day of retries before the job is dead-lettered.
JOB_MAX_ATTEMPTS = 30

_EPISODIC = "episodic"


class DurableForgetUnavailableError(RuntimeError):
    """A durable forget was asked for where its job cannot be committed with the row delete.

    Off Postgres there is no jobs table in the row's transaction and no guard, so recording
    nothing would quietly bring back R9-311: the row goes and the forget is not held anywhere.
    Raised inside the row delete's transaction, so the delete rolls back.
    """


class ConversationMemoryForgetPayload(JobPayload):
    """The persona, the conversation, and the chunk ids to forget. Ids only, never content."""

    persona_id: str
    conversation_id: str
    chunk_ids: list[str]


def idempotency_key(payload: ConversationMemoryForgetPayload) -> str:
    """One forget per conversation: a conversation's row can be deleted only once."""
    return f"{JOB_TYPE}:{payload.conversation_id}"


class EpisodicForgetStore(Protocol):
    """The two calls the forget makes on a :class:`persona.stores.episodic.EpisodicStore`."""

    def get_all(self, persona_id: str) -> Sequence[_Chunk]: ...

    def remove_documents(self, persona_id: str, doc_ids: list[str]) -> None: ...


class _Chunk(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def metadata(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class MemoryForgetPlan:
    """How this delete forgets its episodic memory.

    ``durable``: a worker here runs :data:`JOB_TYPE`, so the forget is recorded as a job in the
    row delete's transaction. ``record``: the in-transaction step (:func:`record`, bound to one
    conversation), handed the ids found so far; returns the stamped ids it captured.
    """

    durable: bool
    record: Callable[[Connection, Sequence[str]], list[str]]


def episodic_ids(
    store: EpisodicForgetStore,
    *,
    persona_id: str,
    conversation_id: str,
    message_texts: Sequence[str],
    match_texts: Callable[[str, list[str], float], list[str]] | None,
    match_floor: float,
) -> list[str]:
    """This conversation's chunks: stamped with it, plus legacy ones its messages content-match.

    The Spec K11 union (D-K11-9). ``match_texts`` matches persona-wide, so its result is kept
    to LEGACY chunks only (no ``conversation_id`` key at all): a chunk stamped with another
    conversation is never swept in for a similar text, and one stamped with this conversation
    is already in the exact set. Reads only.
    """
    chunks = store.get_all(persona_id)
    exact = {c.id for c in chunks if c.metadata.get("conversation_id") == conversation_id}
    legacy: set[str] = set()
    if message_texts and match_texts is not None:
        stamped = {c.id for c in chunks if "conversation_id" in c.metadata}
        legacy = {
            chunk_id
            for chunk_id in match_texts(persona_id, list(message_texts), match_floor)
            if chunk_id not in stamped
        }
    return sorted(exact | legacy)


def remove(store: EpisodicForgetStore, persona_id: str, ids: Sequence[str]) -> int:
    """Forget these chunks and what was derived from them. Idempotent; returns the ids asked."""
    if not ids:
        return 0
    store.remove_documents(persona_id, sorted(set(ids)))
    return len(set(ids))


def record(
    conn: Connection,
    ids: Sequence[str],
    *,
    owner_id: str,
    conversation_id: str,
    durable: bool,
) -> list[str]:
    """Capture, mark and (when ``durable``) enqueue, in the row delete's transaction.

    Returns the ids of the chunks stamped with the conversation, captured under its row lock.
    Postgres only: ``[]`` elsewhere, and when the conversation is not the owner's (the delete
    then answers 404 and rolls back). ``durable`` off Postgres raises
    :class:`DurableForgetUnavailableError` rather than record nothing.
    """
    if conn.dialect.name != "postgresql":
        if durable:
            msg = "a durable conversation forget needs Postgres"
            raise DurableForgetUnavailableError(msg)
        return []
    hold_owners(conn, owner_id=owner_id, conversation_id=conversation_id)
    persona_id = conn.execute(
        select(conversations_t.c.persona_id)
        .where(conversations_t.c.id == conversation_id, conversations_t.c.owner_id == owner_id)
        .with_for_update()
    ).scalar()
    if persona_id is None:
        return []
    captured = _stamped_ids(conn, persona_id=str(persona_id), conversation_id=conversation_id)
    conn.execute(
        pg_insert(forgotten_t)
        .values(conversation_id=conversation_id, owner_id=owner_id, persona_id=persona_id)
        .on_conflict_do_nothing(index_elements=["conversation_id"])
    )
    if durable:
        payload = ConversationMemoryForgetPayload(
            persona_id=str(persona_id),
            conversation_id=conversation_id,
            chunk_ids=sorted({*ids, *captured}),
        )
        enqueue_on(
            conn,
            type=JOB_TYPE,
            owner_id=owner_id,
            payload=payload.model_dump(),
            idempotency_key=idempotency_key(payload),
            scheduled_at=datetime.now(UTC) + JOB_DELAY,
            max_attempts=JOB_MAX_ATTEMPTS,
        )
    return captured


def hold_owners(conn: Connection, *, owner_id: str, conversation_id: str) -> None:
    """Hold the owner's ``users`` row, then the conversation's persona, ``FOR KEY SHARE``.

    The mark and the job reference both rows, so their inserts take these locks anyway. Taken
    here, before the conversation's row is locked, the order is the erasures': an account
    erasure locks ``users`` and a persona erasure the persona before either reaches the
    conversation through the cascade. Taken after, a forget and an erasure running together
    could each hold what the other waits for. A no-op re-lock when the graph step took them.
    """
    # ``read`` with ``key_share`` is ``FOR KEY SHARE`` (``key_share`` alone: ``FOR NO KEY UPDATE``).
    conn.execute(
        select(users_t.c.id)
        .where(users_t.c.id == owner_id)
        .with_for_update(read=True, key_share=True)
    )
    persona_of = (
        select(conversations_t.c.persona_id)
        .where(conversations_t.c.id == conversation_id, conversations_t.c.owner_id == owner_id)
        .scalar_subquery()
    )
    conn.execute(
        select(personas_t.c.id)
        .where(personas_t.c.id == persona_of, personas_t.c.owner_id == owner_id)
        .with_for_update(read=True, key_share=True, of=personas_t)
    )


def _stamped_ids(conn: Connection, *, persona_id: str, conversation_id: str) -> list[str]:
    """Every version of every episodic chain a row stamped with the conversation belongs to.

    Not only the stamped rows: a rollback writes a new version that need not carry the stamp, and
    a gist built on it is reached on a retry only through its id (the raw rows being gone by
    then). Versions share a ``logical_id``.
    """
    chunks = memory_chunks_t.c
    stamped = chunks.metadata["conversation_id"].as_string() == conversation_id
    scope = (chunks.persona_id == persona_id, chunks.kind == _EPISODIC)
    chains = select(chunks.logical_id).where(*scope, stamped, chunks.logical_id.is_not(None))
    rows = conn.execute(
        select(chunks.id).where(*scope, or_(stamped, chunks.logical_id.in_(chains)))
    ).scalars()
    return sorted(str(chunk_id) for chunk_id in rows)


def is_forgotten(conn: Connection, *, owner_id: str, persona_id: str, conversation_id: str) -> bool:
    """Whether this owner forgot this conversation of this persona (the job's scope check)."""
    found = conn.execute(
        select(forgotten_t.c.conversation_id).where(
            forgotten_t.c.conversation_id == conversation_id,
            forgotten_t.c.owner_id == owner_id,
            forgotten_t.c.persona_id == persona_id,
        )
    ).first()
    return found is not None


def job_attempt(conn: Connection, job_id: str) -> int | None:
    """Which attempt of the job this is (the claim counts it), or ``None`` if it is not found."""
    attempt = conn.execute(select(jobs_t.c.attempt).where(jobs_t.c.id == job_id)).scalar()
    return None if attempt is None else int(attempt)


def plan(*, owner_id: str, conversation_id: str, durable: bool) -> MemoryForgetPlan:
    """The plan for one conversation's delete."""

    def _record(conn: Connection, ids: Sequence[str]) -> list[str]:
        return record(
            conn, ids, owner_id=owner_id, conversation_id=conversation_id, durable=durable
        )

    return MemoryForgetPlan(durable=durable, record=_record)
