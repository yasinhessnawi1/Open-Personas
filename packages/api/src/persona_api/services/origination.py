"""The RLS-scoped recorder for persona-initiated (originated) messages (Spec C0, T4).

:class:`OriginationRecorder` is the persona-api implementation of persona-core's
``OriginatedMessageRecorder`` seam (D-C0-1): it persists an originated message as a
first-class conversation + episodic citizen, owner-scoped, so the within-runtime
``Originator`` (and later direction 4) can record before delivery.

Ownership (criterion 9, D-C0-X-rls-ownership) is enforced by an **explicit
owner-id predicate** — a persona may originate ONLY to the user who owns it — which
raises :class:`~persona.errors.OriginationForbiddenError` *before any write* (so a
cross-tenant attempt leaves no half-written row). This is deliberate: RLS alone
would *silently* zero-row a cross-tenant write (no error), which the caller would
mistake for success and then deliver a non-persisted message. The explicit check
gives the clean failure; RLS (the Spec-08 ``current_user_id`` contextvar mechanism,
edition-gated to cloud exactly as the personas.py background-task precedent) is the
production backstop. ZERO coupling to A0's worker — only the Spec-08 contextvar +
the canonical ``messages``/``conversations``/``personas`` tables.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from persona.errors import OriginationForbiddenError
from persona.schema.chunks import (
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
    mint_chunk_id,
)
from persona.schema.conversation import ORIGINATED_METADATA_KEY
from sqlalchemy import insert, select

from persona_api.config import Edition
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.middleware.rls_context import current_user_id

if TYPE_CHECKING:
    from persona.schema.origination import OriginatedMessage
    from persona.stores.episodic import EpisodicStore
    from sqlalchemy import Engine

_ORIGINATION_ACTOR = "origination"


class OriginationRecorder:
    """Persist an originated message into a conversation + episodic, owner-scoped.

    Implements persona-core's ``OriginatedMessageRecorder`` structural seam. Holds
    no state beyond its injected collaborators (DI; no globals).

    Args:
        rls_engine: The per-app RLS-scoped engine (Spec 08, D-08-1) — the same
            engine the request path and the runtime's stores run on.
        episodic_store: The episodic store the originated message is written to,
            the same path a reply uses (criterion 2). RLS-scoped via ``rls_engine``.
        edition: The open-core edition (Spec 33). On ``cloud`` the owner contextvar
            is bound around the writes so the pool's RLS listener scopes them;
            ``community`` runs a listener-less single-owner engine (no GUC).
    """

    def __init__(
        self,
        *,
        rls_engine: Engine,
        episodic_store: EpisodicStore,
        edition: Edition,
    ) -> None:
        self._engine = rls_engine
        self._episodic = episodic_store
        self._edition = edition

    async def record(self, message: OriginatedMessage) -> str:
        """Persist ``message`` and return its resolved conversation id.

        Enforces ownership (raises :class:`OriginationForbiddenError` on a
        cross-tenant target, before any write → no half-write), starts a
        conversation when ``message.conversation_id`` is ``None`` (D-C0-3), writes
        the originated message row (``role="assistant"``, ``originated=true``), then
        the assistant-only episodic chunk.
        """
        owner_id = message.owner_user_id
        persona_id = message.persona.persona_id
        reset_token = None
        if self._edition is Edition.cloud:
            reset_token = current_user_id.set(owner_id)
        try:
            conversation_id = self._persist_message(
                message, owner_id=owner_id, persona_id=persona_id
            )
            self._write_episodic(persona_id, message, conversation_id)
            return conversation_id
        finally:
            if reset_token is not None:
                current_user_id.reset(reset_token)

    def _persist_message(
        self, message: OriginatedMessage, *, owner_id: str, persona_id: str
    ) -> str:
        """Ownership-check → resolve/start conversation → insert the row (one txn).

        The ownership guard and (when supplied) the conversation guard raise before
        any INSERT, and the whole body is one transaction, so a rejected
        origination rolls back to zero rows — no half-write.
        """
        with self._engine.begin() as conn:
            owns_persona = conn.execute(
                select(personas_t.c.id).where(
                    personas_t.c.id == persona_id,
                    personas_t.c.owner_id == owner_id,
                )
            ).first()
            if owns_persona is None:
                raise OriginationForbiddenError(
                    "a persona may only originate to the user who owns it",
                    context={"persona_id": persona_id, "target": owner_id},
                )

            conversation_id = message.conversation_id
            if conversation_id is None:
                conversation_id = f"conv_{uuid.uuid4().hex}"
                conn.execute(
                    insert(conversations_t).values(
                        id=conversation_id,
                        owner_id=owner_id,
                        persona_id=persona_id,
                        title="",
                    )
                )
            else:
                owns_conversation = conn.execute(
                    select(conversations_t.c.id).where(
                        conversations_t.c.id == conversation_id,
                        conversations_t.c.owner_id == owner_id,
                        conversations_t.c.persona_id == persona_id,
                    )
                ).first()
                if owns_conversation is None:
                    raise OriginationForbiddenError(
                        "originated message targets a conversation not owned by the "
                        "persona's owner",
                        context={"conversation_id": conversation_id, "target": owner_id},
                    )

            conn.execute(
                insert(messages_t).values(
                    id=f"msg_{uuid.uuid4().hex}",
                    conversation_id=conversation_id,
                    role="assistant",
                    content=message.content,
                    originated=True,
                    created_at=message.created_at,
                )
            )
        return conversation_id

    def _write_episodic(
        self, persona_id: str, message: OriginatedMessage, conversation_id: str
    ) -> None:
        """Write the assistant-only originated episodic chunk (criterion 2, D-C0-3).

        No USER half (no preceding user turn) — the persona's memory honestly
        reflects that it reached out. Same store path a reply uses; RLS-scoped via
        ``rls_engine``.

        ``conversation_id`` — the resolved id :meth:`_persist_message` returns (a
        new or an existing conversation, always known by this point) — stamps
        ``metadata["conversation_id"]`` (Spec K11, D-K11-9), so an originated
        chunk cascades on ``DELETE .../conversations/{id}?forget_memory=true``
        exactly like a reply's chunk does.
        """
        # Minted uuidv7 id (K8-D-6) — the former store-count index was an
        # O(N) read per write and raced under concurrent writers.
        chunk_id = mint_chunk_id(persona_id, "episodic")
        now = message.created_at
        self._episodic.write(
            persona_id,
            [
                PersonaChunk(
                    id=chunk_id,
                    text=f"ASSISTANT (originated): {message.content}",
                    metadata={
                        "importance": "0.5",
                        ORIGINATED_METADATA_KEY: "true",
                        "conversation_id": conversation_id,
                    },
                    created_at=now,
                    provenance=ChunkProvenance(
                        source=WriteSource.SYSTEM,
                        logical_id=chunk_id,
                        version=1,
                        written_at=now,
                        written_by=_ORIGINATION_ACTOR,
                    ),
                ),
            ],
            source=WriteSource.SYSTEM,
            written_by=_ORIGINATION_ACTOR,
        )
