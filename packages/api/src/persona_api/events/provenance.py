"""The frozen A7 → A6 provenance contract (Spec A7, T8; A7-D-9).

A6 renders "why did this happen?" — *"ran because: an email from landlord@… arrived"* — over two
durable surfaces A7 already writes. This module is the **frozen vocabulary** of that contract, as
importable constants A6 builds against; the shape test (``test_a7_provenance_contract.py``) drives
the REAL emitters and asserts they match these constants, so the contract cannot drift silently
(a renamed action or a changed metadata key fails the test, not A6 at runtime).

Two routes (A7-D-9):

- **Route (a) — the audit trail** (``audit_log`` rows, the task-detail history A6 reads). Each row
  is ``(user_id, action, target, metadata: dict[str, str])``. The action vocabulary is CLOSED
  (:data:`EVENT_TRIGGER_AUDIT_ACTIONS`); ``metadata`` values are always strings (the column is
  ``dict[str, str]`` — structured chains/counts are serialised). The four A6-facing actions carry
  the render substance; three sibling forensic actions round out the closed set (A6 may render them
  generically or skip them — they are never a surprise).
- **Route (b) — the leg-attached identity** (the :class:`~persona.tasks.EventFire` riding the fired
  leg's trigger — the leg timeline + the morning-Review digest). :data:`EVENT_FIRE_IDENTITY_FIELDS`
  is the frozen field set; A6 renders ``"ran because: {human}"`` from the durable ``human`` field
  (reports are pure projections — the identity lives on the leg, A7-D-6).

The single canonical render string A6 uses is ``human`` — it appears BOTH on the fired audit row
(``metadata["human"]``) and on the leg's ``EventFire.human``, so both surfaces render identically
without A6 recomputing it. (The A7-D-9 draft named a separate ``sender`` field; it is folded into
``human`` — *"a message from {sender} arrived"* — which is the one string the user reads.)
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "CANDIDATE_WELLBEING_DROPPED_METADATA_KEYS",
    "DEPTH_EXCEEDED_METADATA_KEYS",
    "EVENT_FIRE_IDENTITY_FIELDS",
    "EVENT_TRIGGER_AUDIT_ACTIONS",
    "EVENT_TRIGGER_CANDIDATE_WELLBEING_DROPPED",
    "EVENT_TRIGGER_DEPTH_EXCEEDED",
    "EVENT_TRIGGER_FIRED",
    "EVENT_TRIGGER_LOOP_REFUSED",
    "EVENT_TRIGGER_STORM_DROPPED",
    "EVENT_TRIGGER_TASK_MISSING",
    "EVENT_TRIGGER_UNGROUNDABLE",
    "FIRED_METADATA_KEYS",
    "LOOP_REFUSED_METADATA_KEYS",
    "PROVENANCE_RENDER_FIELD",
    "STORM_DROPPED_METADATA_KEYS",
]

# --- Route (a): the closed audit-action vocabulary ---------------------------------------------

#: A trigger fired a leg (door a) or enqueued a candidate (door b). ``target`` is the fired task id
#: (door a) / the trigger id (door b). The A6 render row.
EVENT_TRIGGER_FIRED: Final = "event_trigger.fired"
#: A self-triggering cycle was refused (the trigger was already in the event's causal chain).
#: ``target`` = trigger id.
EVENT_TRIGGER_LOOP_REFUSED: Final = "event_trigger.loop_refused"
#: A fire was dropped over the R7 day-cap (storm safety). ``target`` = trigger id. Also surfaced to
#: the user via a durable P6 bell (never silent).
EVENT_TRIGGER_STORM_DROPPED: Final = "event_trigger.storm_dropped"
#: A door-b candidate was dropped because its grounding is a wellbeing-gated subject (criterion 8,
#: the handler seam). ``target`` = trigger id.
EVENT_TRIGGER_CANDIDATE_WELLBEING_DROPPED: Final = "event_trigger.candidate_wellbeing_dropped"

# Sibling forensic actions — part of the closed vocabulary, not primary A6 render rows.
#: A distinct-trigger cycle hit ``max_chain_depth`` (the belt-and-braces loop backstop).
EVENT_TRIGGER_DEPTH_EXCEEDED: Final = "event_trigger.depth_exceeded"
#: Door a's target task vanished between match and fire (a CASCADE race — benign).
EVENT_TRIGGER_TASK_MISSING: Final = "event_trigger.task_missing"
#: A door-b match had no citable conversation/task grounding (e.g. a link event; A7-D-7).
EVENT_TRIGGER_UNGROUNDABLE: Final = "event_trigger.ungroundable"

#: The CLOSED set of audit actions A7 writes. A6 renders these or falls back generically; a NEW
#: action is a contract change (the shape test asserts every emitted action is in this set).
EVENT_TRIGGER_AUDIT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        EVENT_TRIGGER_FIRED,
        EVENT_TRIGGER_LOOP_REFUSED,
        EVENT_TRIGGER_STORM_DROPPED,
        EVENT_TRIGGER_CANDIDATE_WELLBEING_DROPPED,
        EVENT_TRIGGER_DEPTH_EXCEEDED,
        EVENT_TRIGGER_TASK_MISSING,
        EVENT_TRIGGER_UNGROUNDABLE,
    }
)

# --- Route (a): the per-action metadata key schemas (values are always strings) ----------------

#: ``event_trigger.fired`` — the render row. ``human`` is the "ran because" string.
FIRED_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {"trigger_id", "event_kind", "event_id", "human", "door"}
)
#: ``event_trigger.loop_refused`` — ``chain`` is the comma-joined causal chain.
LOOP_REFUSED_METADATA_KEYS: Final[frozenset[str]] = frozenset({"event_id", "chain"})
#: ``event_trigger.storm_dropped`` — ``reason`` (e.g. ``over_day_cap``) + the coalesced count.
STORM_DROPPED_METADATA_KEYS: Final[frozenset[str]] = frozenset({"reason", "coalesced_count"})
#: ``event_trigger.candidate_wellbeing_dropped`` — ``grounding`` is ``{kind}/{ref}``.
CANDIDATE_WELLBEING_DROPPED_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {"event_id", "grounding"}
)
#: ``event_trigger.depth_exceeded`` — ``depth`` is the chain length at refusal.
DEPTH_EXCEEDED_METADATA_KEYS: Final[frozenset[str]] = frozenset({"event_id", "depth"})

# --- Route (b): the leg-attached EventFire identity --------------------------------------------

#: The frozen field set of :class:`~persona.tasks.EventFire` — the identity that rides the fired
#: leg's trigger (leg timeline + Review digest). A6 renders from ``human``.
EVENT_FIRE_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"trigger_id", "event_kind", "event_id", "fired_at", "human", "causal_chain"}
)

#: The one canonical render field — the same string on the audit row AND the leg identity, so both
#: A6 surfaces render ``"ran because: {human}"`` identically.
PROVENANCE_RENDER_FIELD: Final = "human"
