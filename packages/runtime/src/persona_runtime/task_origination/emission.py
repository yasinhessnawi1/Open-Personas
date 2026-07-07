"""Emitting the confirmed contract across the runtime→api boundary (Spec A4, T6; A4-D-X).

On the confirm turn the runtime has everything — the canonical draft, the built contract, the
parsed schedule — but it cannot create anything (runtime ⊥ api). So it emits a single
``task_originated`` :class:`RunEvent` (data only); the chat-turn worker consumes it and the api
``OriginationService`` does the create. This module builds that event from a confirmed draft and
computes the **content hash** that is the idempotency *fallback* key (the primary key is the
confirm turn's ``assistant_message_id``).

The hash is taken over the **canonicalised** draft (grants in a stable order), so two
confirmations of the same contract hash identically — the property A4-T4's
``test_canonicalize_draft_sorts_grants_deterministically`` already pinned.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from persona_runtime.agentic.events import RunEvent
from persona_runtime.task_origination.draft import build_contract, canonicalize_draft

if TYPE_CHECKING:
    from persona_runtime.task_origination.draft import ContractDraft

__all__ = ["build_task_originated_event", "draft_content_hash"]


def draft_content_hash(draft: ContractDraft) -> str:
    """A stable SHA-256 over the canonical draft — the content-dedup fallback key.

    Canonicalises first so grant ordering cannot change the hash; two confirmations of the
    same contract content produce the same hash.
    """
    canonical = canonicalize_draft(draft)
    return hashlib.sha256(canonical.model_dump_json().encode("utf-8")).hexdigest()


def _schedule_payload(draft: ContractDraft) -> dict[str, Any]:
    """The JSON-safe parsed-cadence payload (empty when the draft has no schedule)."""
    if draft.schedule is None:
        return {}
    return draft.schedule.model_dump(mode="json")


def _trigger_payload(draft: ContractDraft) -> dict[str, Any]:
    """The JSON-safe trigger spec (empty when the draft has no trigger — A7). Schedule XOR this."""
    if draft.trigger is None:
        return {}
    return draft.trigger.model_dump(mode="json")


def build_task_originated_event(
    *,
    draft: ContractDraft,
    owner_id: str,
    persona_id: str,
    persona_name: str,
    conversation_id: str,
    assistant_message_id: str,
) -> RunEvent:
    """Build the ``task_originated`` event from a confirmed draft (A4-D-X).

    Canonicalises the draft, builds the frozen contract (matrix + cap included), and stamps the
    event with the dedup anchors: ``assistant_message_id`` (primary) and the content hash
    (fallback). Emitting this is the runtime's last act in the contract flow; the api creates.
    """
    canonical = canonicalize_draft(draft)
    contract = build_contract(canonical)
    return RunEvent.task_originated(
        owner_id=owner_id,
        persona_id=persona_id,
        persona_name=persona_name,
        conversation_id=conversation_id,
        assistant_message_id=assistant_message_id,
        contract=contract.model_dump(mode="json"),
        schedule=_schedule_payload(canonical),
        trigger=_trigger_payload(canonical),
        draft_hash=draft_content_hash(canonical),
    )
