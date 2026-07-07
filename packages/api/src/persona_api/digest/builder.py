"""The one shared morning-digest builder (Spec A6, B5; A6-D-2).

A6 owns it, C0 consumes it — one builder, two renderings (the C6/K5 one-mechanism discipline).
:func:`build_morning_digest` composes a render-agnostic :class:`MorningDigest` from the durable
A0–A5 state, RLS-scoped per owner; the web review serialises the model, and C0's morning message
renders it via :func:`render_digest_message` — one source of truth for "what happened overnight".

Ordering is fixed (A6-D-2): **waiting-on-you → stuck → done → initiatives**, then a compact upcoming
strip. The under-a-minute promise (A6-R-1) is kept by per-section caps + an honest ``overflow``
count (never a wall of text). Deferred chatter is a **secondary** input folded into "done" — the
main sections never depend on it (A6-D-10). The A7 "ran because" line is sourced from the
``event_trigger.fired`` audit provenance (route (a), A7-D-9): a done/stuck task that ran from an
event carries its ``human`` string; a task that never fired from an event leaves it ``None``.
"""

from __future__ import annotations

from datetime import datetime, timedelta  # noqa: TC003 — runtime Pydantic field types
from typing import TYPE_CHECKING

import yaml as _yaml
from persona.tasks import TaskState
from persona.tasks.reports import build_completion_report, build_stuck_report
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from persona_api.approvals.store import ApprovalStore
from persona_api.db.engine import rls_connection
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import personas as personas_t
from persona_api.initiative.store import InitiativeLedger
from persona_api.services import occurrences_service
from persona_api.tasks.reader import APITaskStateReader
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.digest.store import DeferredDigestItem

__all__ = [
    "DigestItem",
    "DigestRef",
    "DigestSection",
    "MorningDigest",
    "UpcomingItem",
    "build_morning_digest",
    "render_digest_message",
]

#: Per-section item caps — the under-a-minute bound (A6-R-1). Over the cap → an honest overflow.
_SECTION_CAP = {"waiting": 6, "stuck": 6, "done": 6, "initiatives": 3}
_UPCOMING_CAP = 6
_TERMINAL_LIMIT = 25
_UPCOMING_HORIZON = timedelta(days=2)
#: The A7 fired-audit action; its ``target`` is the task_id and ``metadata["human"]`` is the one
#: canonical "ran because: {human}" string A6 renders (A7-D-9, route (a)).
_EVENT_FIRED_ACTION = "event_trigger.fired"
_SECTION_ORDER = ("waiting", "stuck", "done", "initiatives")
_SECTION_LABEL = {
    "waiting": "Waiting on you",
    "stuck": "Stuck",
    "done": "Done overnight",
    "initiatives": "Noticed",
}


class DigestRef(BaseModel):
    """The durable referent a Review line deep-links to (A6-D-6; criterion-10 glance→approve).

    ``kind="approval"`` → the proposal (``/approvals?id=``); ``kind="task"`` → the task
    (``/tasks/{id}``). ``None`` on an item with no actionable target (a noticed initiative, a
    deferred one-liner).
    """

    model_config = ConfigDict(frozen=True)

    kind: str  # approval | task
    id: str


class DigestItem(BaseModel):
    """One line in a section — persona-voiced where the persona speaks; verbatim-safe as text."""

    model_config = ConfigDict(frozen=True)

    persona_id: str
    title: str
    detail: str = ""
    #: The durable referent for a per-item deep-link (A6-D-6); ``None`` when there is no target.
    ref: DigestRef | None = None
    #: A7 provenance ("ran because: …") from the ``event_trigger.fired`` audit ``human`` (A7-D-9);
    #: ``None`` when the item's task never ran from an event trigger.
    ran_because: str | None = None


class DigestSection(BaseModel):
    """A priority-ordered section, capped for the one-minute read (with an honest overflow)."""

    model_config = ConfigDict(frozen=True)

    kind: str  # waiting | stuck | done | initiatives
    items: tuple[DigestItem, ...]
    overflow: int = 0  # how many more beyond the cap ("+N more")


class UpcomingItem(BaseModel):
    """One entry in the compact upcoming strip (from A8's occurrences — the engine's own fires)."""

    model_config = ConfigDict(frozen=True)

    fire_at: datetime
    persona_id: str | None
    label: str


class MorningDigest(BaseModel):
    """The render-agnostic morning review — the same content the web surface and C0 both render."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    sections: tuple[DigestSection, ...]  # ordered; empty sections omitted
    upcoming: tuple[UpcomingItem, ...]
    total_spent_micros: int = 0  # what the overnight work cost
    persona_names: dict[str, str] = {}  # persona_id → display name (self-contained for rendering)


def _first(values: Sequence[str]) -> str:
    return values[0] if values else ""


def _section(kind: str, items: list[DigestItem]) -> DigestSection | None:
    """Cap the section and record the overflow; ``None`` when there is nothing to show."""
    if not items:
        return None
    cap = _SECTION_CAP[kind]
    return DigestSection(kind=kind, items=tuple(items[:cap]), overflow=max(0, len(items) - cap))


def _ran_because_for_tasks(
    engine: Engine, owner_id: str, task_ids: Sequence[str]
) -> dict[str, str]:
    """Map each task_id → its A7 "ran because: {human}" string, when the task ran from an event.

    A task fired by an A7 event trigger (door-a) writes an ``event_trigger.fired`` audit row keyed
    ``target = task_id`` with ``metadata["human"]`` (A7-D-9). The most recent such row per task is
    the provenance A6 renders. Tasks that never ran from an event have no row → absent from the map
    (``ran_because`` stays ``None``). One RLS-scoped query over the ``(target, action)`` index; the
    ``DISTINCT ON (target)`` keeps the newest fire per task. Empty ``task_ids`` → no query.
    """
    if not task_ids:
        return {}
    stmt = (
        select(audit_log_t.c.target, audit_log_t.c.metadata)
        .where(
            audit_log_t.c.user_id == owner_id,  # defensive scope (audit_log is a platform table)
            audit_log_t.c.action == _EVENT_FIRED_ACTION,
            audit_log_t.c.target.in_(list(task_ids)),
        )
        .order_by(audit_log_t.c.target, audit_log_t.c.created_at.desc())
        .distinct(audit_log_t.c.target)
    )
    out: dict[str, str] = {}
    with rls_connection(engine, owner_id) as conn:
        for row in conn.execute(stmt):
            human = (row.metadata or {}).get("human")
            if human:
                out[row.target] = str(human)
    return out


def build_morning_digest(
    engine: Engine,
    *,
    owner_id: str,
    config: APIConfig,
    now: datetime,
    deferred: Sequence[DeferredDigestItem] = (),
) -> MorningDigest:
    """Compose the owner's morning digest from durable A0–A5 state (RLS-scoped per source).

    ``deferred`` is the already-consumed over-cap chatter (a secondary input, folded into "done").
    Pure composition over the RLS engine + ``config`` (for occurrences) — no RuntimeFactory.
    """
    waiting = [
        DigestItem(
            persona_id=p.persona_id,
            title=p.description,
            ref=DigestRef(kind="approval", id=p.proposal_id),
        )
        for p in ApprovalStore(engine).list_pending_for_owner(owner_id)
    ]

    reader = APITaskStateReader(TaskStore(engine), CheckpointStore(engine), owner_id)
    done: list[DigestItem] = []
    stuck: list[DigestItem] = []
    spent = 0
    terminal_tasks = list(reader.list_recent_terminal(limit=_TERMINAL_LIMIT))
    # A7 provenance (A7-D-9): the "ran because: {human}" for any of these that ran from an event
    # trigger — one RLS-scoped audit read keyed by task_id (absent ⇒ ``ran_because`` stays None).
    ran_because = _ran_because_for_tasks(engine, owner_id, [t.id for t in terminal_tasks])
    for task in terminal_tasks:
        checkpoint = reader.get_latest_checkpoint(task.id)
        if task.state is TaskState.COMPLETED:
            completion = build_completion_report(task, checkpoint, now=now)
            spent += completion.total_micros
            done.append(
                DigestItem(
                    persona_id=task.persona_id,
                    title=task.contract.goal,
                    detail=_first(completion.conclusions),
                    ref=DigestRef(kind="task", id=task.id),
                    ran_because=ran_because.get(task.id),
                )
            )
        elif task.state is TaskState.FAILED:
            cause = (checkpoint.blocked_on if checkpoint is not None else None) or ""
            stuck_report = build_stuck_report(task, checkpoint, cause=cause, now=now)
            spent += stuck_report.total_micros
            stuck.append(
                DigestItem(
                    persona_id=task.persona_id,
                    title=task.contract.goal,
                    detail=stuck_report.cause,
                    ref=DigestRef(kind="task", id=task.id),
                    ran_because=ran_because.get(task.id),
                )
            )
    # secondary: the deferred chatter, as one-liners under "done" (never load-bearing).
    done.extend(DigestItem(persona_id=d.persona_id, title=d.content) for d in deferred)

    initiatives = [
        DigestItem(
            persona_id=n.persona_id,
            title=n.candidate.observation,
            detail=n.candidate.why_now,
        )
        for n in InitiativeLedger(engine).held_for_owner(owner_id)
    ]

    occ = occurrences_service.list_occurrences(
        engine, owner_id=owner_id, from_=now, to=now + _UPCOMING_HORIZON, config=config
    )
    upcoming = tuple(
        UpcomingItem(fire_at=o.fire_at, persona_id=o.persona_id, label=o.human_terms)
        for o in occ.occurrences[:_UPCOMING_CAP]
    )

    by_kind = {"waiting": waiting, "stuck": stuck, "done": done, "initiatives": initiatives}
    sections = tuple(
        s for kind in _SECTION_ORDER if (s := _section(kind, by_kind[kind])) is not None
    )
    return MorningDigest(
        generated_at=now,
        sections=sections,
        upcoming=upcoming,
        total_spent_micros=spent,
        persona_names=_resolve_names(engine, owner_id, sections, upcoming),
    )


def _persona_name(raw_yaml: str) -> str:
    """The persona's display name from its YAML (``identity.name``); ``""`` if absent/malformed."""
    try:
        data = _yaml.safe_load(raw_yaml) or {}
        return (data.get("identity") or {}).get("name") or ""
    except Exception:  # noqa: BLE001 — a malformed persona YAML must not break the digest
        return ""


def _resolve_names(
    engine: Engine,
    owner_id: str,
    sections: tuple[DigestSection, ...],
    upcoming: tuple[UpcomingItem, ...],
) -> dict[str, str]:
    """Resolve each persona's display name so the digest is self-contained for both renderings.

    Self-scoped (``rls_connection``) like every other read here, so the builder is correct even off
    a request (a background C0 build) — not reliant on an ambient GUC. One query; id is fallback.
    """
    ids = {item.persona_id for s in sections for item in s.items}
    ids |= {u.persona_id for u in upcoming if u.persona_id is not None}
    names: dict[str, str] = {persona_id: persona_id for persona_id in ids}
    if not ids:
        return names
    with rls_connection(engine, owner_id) as conn:
        rows = (
            conn.execute(select(personas_t.c.id, personas_t.c.yaml).where(personas_t.c.id.in_(ids)))
            .mappings()
            .all()
        )
    for row in rows:
        names[str(row["id"])] = _persona_name(str(row["yaml"])) or str(row["id"])
    return names


def render_digest_message(digest: MorningDigest) -> str:
    """Render the digest as C0's morning message (the second rendering; same source of truth)."""
    lines: list[str] = []
    for section in digest.sections:
        lines.append(f"{_SECTION_LABEL.get(section.kind, section.kind)}:")
        for item in section.items:
            name = digest.persona_names.get(item.persona_id, item.persona_id)
            line = f"  · {name}: {item.title}"
            if item.detail:
                line += f" — {item.detail}"
            if item.ran_because:
                line += f" (ran because: {item.ran_because})"
            lines.append(line)
        if section.overflow:
            lines.append(f"  (+{section.overflow} more)")
    if digest.upcoming:
        lines.append("Upcoming:")
        for u in digest.upcoming:
            name = digest.persona_names.get(u.persona_id or "", u.persona_id or "")
            suffix = f" ({name})" if name else ""
            lines.append(f"  · {u.label}{suffix}")
    return "\n".join(lines) if lines else "Nothing to report — a quiet night."
