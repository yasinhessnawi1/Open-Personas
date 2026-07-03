"""Within-runtime origination — a run's conclusion becomes a delivered message
(Spec C0, T7, criterion 7).

The direction-2 exercise of the primitive: an agentic run the user initiated, at
its conclusion, produces an originated message ("I've finished the task you asked
for") that is **persisted** (conversation + episodic, the recorder) AND **delivered
inline on that run's own already-open SSE stream** (the web deliverer, via a
:class:`LiveSessionSink` backed by the run's real event queue). This needs no
direction-4 infrastructure — the run is live and streaming.

The wiring is the REAL run stream, not a fake: :class:`RunStreamSink` pushes a
``persona_originated`` :class:`~persona_runtime.agentic.events.RunEvent` onto the
run's ``RunHandle`` event queue — the same queue the ``/events`` SSE endpoint
drains — so the open client renders it inline. The capability driven here is the
*same* :class:`~persona.originator.Originator` direction 4 will drive autonomously
(criterion 8); only the *trigger* (the run's conclusion) differs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml as _yaml
from persona.audit import AuditLogger, JSONLAuditLogger
from persona.originator import Originator
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona.stores.episodic import EpisodicStore
from persona_runtime.agentic.events import RunEvent
from sqlalchemy import select

from persona_api.db.models import personas as personas_t
from persona_api.services.delivery_router import DeliveryRouter
from persona_api.services.origination import OriginationRecorder
from persona_api.services.web_deliverer import WebAppDeliverer

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.backend import Backend
    from persona_runtime.agentic.run import Run
    from sqlalchemy import Engine

    from persona_api.background.run_worker import RunHandle
    from persona_api.config import Edition

#: The SSE event type carrying an originated message inline on a run's stream.
ORIGINATED_EVENT_TYPE = "persona_originated"

_log_actor = "within_runtime_origination"


class RunStreamSink:
    """A live sink backed by a run's real event queue (the within-runtime stream)."""

    def __init__(self, handle: RunHandle) -> None:
        self._handle = handle

    async def push(self, message: OriginatedMessage) -> None:
        """Push the originated message as a ``persona_originated`` event onto the
        run's open SSE queue — the client renders it inline."""
        event = RunEvent(
            type=ORIGINATED_EVENT_TYPE,
            step=-1,
            data={
                "content": message.content,
                "persona_id": message.persona.persona_id,
                "persona_name": message.persona.display_name,
                "visual_ref": message.persona.visual_ref or "",
                "conversation_id": message.conversation_id or "",
            },
            timestamp=message.created_at,
        )
        await self._handle.on_event(event)


class RunStreamRegistry:
    """A live-session registry bound to one run's handle (the within-runtime case).

    The originated message is produced *inside this very run*, so the run's own
    open stream is the delivery target. Returns the sink only for the run's owner
    (defence-in-depth alongside the recorder's ownership guard).
    """

    def __init__(self, handle: RunHandle) -> None:
        self._handle = handle
        self._sink = RunStreamSink(handle)

    def lookup(self, message: OriginatedMessage) -> RunStreamSink | None:
        if message.owner_user_id == self._handle.owner_id:
            return self._sink
        return None


class WithinRuntimeOriginator:
    """Fires within-runtime origination at a run's conclusion (criterion 7).

    Holds the cross-run collaborators (the engine, the episodic store, the edition);
    builds the per-run delivery chain (bound to the run's open stream) on each call.
    Best-effort by contract: the run worker calls this in a guard that never lets an
    origination failure fail the run (origination is additive — criterion 10).
    """

    def __init__(
        self,
        *,
        rls_engine: Engine,
        memory_backend: Backend,
        edition: Edition,
        audit_root: Path,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._engine = rls_engine
        self._edition = edition
        # Edition-agnostic: the episodic store uses the SAME memory backend the
        # rest of the app does (Chroma for community, Postgres for cloud — Spec 33),
        # so within-runtime origination works in both editions, not just cloud.
        # R5-D-2: prefer the app-selected audit backend (Postgres when
        # multi-worker); audit_root is the byte-unchanged JSONL fallback.
        self._episodic = EpisodicStore(
            backend=memory_backend,
            audit_logger=audit_logger or JSONLAuditLogger(audit_root),
        )

    async def originate_run_conclusion(self, handle: RunHandle, run: Run) -> None:
        """Originate the run's conclusion as a first-class, delivered message.

        Runs under the run worker's owner RLS scope. Starts a conversation
        (D-C0-3 — a run has no conversation), persists the message + episodic, and
        delivers it inline on the run's open stream. No-ops when the run produced
        no output.
        """
        content = run.output
        if not content:
            return
        tag = self._persona_tag(run.persona_id)
        if tag is None:
            return
        sessions = RunStreamRegistry(handle)
        web = WebAppDeliverer(rls_engine=self._engine, sessions=sessions)
        router = DeliveryRouter(deliverers={"web": web}, rls_engine=self._engine)
        recorder = OriginationRecorder(
            rls_engine=self._engine, episodic_store=self._episodic, edition=self._edition
        )
        originator = Originator(recorder=recorder, deliverer=router)
        await originator.originate(
            persona=tag,
            owner_user_id=handle.owner_id,
            content=content,
            created_at=datetime.now(UTC),
        )

    def _persona_tag(self, persona_id: str) -> PersonaIdentityTag | None:
        """Build the persona identity tag from the persona row (RLS-scoped)."""
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(personas_t.c.yaml, personas_t.c.avatar_url).where(
                        personas_t.c.id == persona_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        name = persona_id
        try:
            raw = _yaml.safe_load(row["yaml"]) or {}
            identity = raw.get("identity") or {}
            name = identity.get("name") or persona_id
        except Exception:  # noqa: BLE001 — a malformed YAML must not break delivery
            name = persona_id
        return PersonaIdentityTag(
            persona_id=persona_id, display_name=name, visual_ref=row["avatar_url"]
        )
