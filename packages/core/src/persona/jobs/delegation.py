"""The delegated-turn job contract — the ONE definition both writers share (Spec A9, A9-D-5).

Under the A9 delegation architecture voice **never executes** a task/schedule/autonomy ask with
the mid model; it delegates the **verbatim spoken ask** to the chat pipeline, which re-parses on
the frontier tier and executes through the one audited path. The crossing is a durable A0
``delegated_turn`` job: persona-voice (a peer process that never imports ``persona_api``) enqueues
it with a small raw INSERT beside its raw-SQL peers, and an api-side A0 handler runs the chat turn.

As with ``persona.jobs.synthesis`` (V13 D-4-amended), the *table* stays api-owned but the
**semantic** drift surface collapses to a single definition here in core — the payload shape, the
job-type string, and the idempotency-key recipe — so the two thin writers cannot disagree about
*what* a delegated turn is, only about *how* they INSERT the row (pinned by a bidirectional
column-parity test + the real-worker-transition test).

**The payload carries the verbatim ask, never a parsed draft** (A9-D-5): the frontier is the only
parser whose output is executed — the mid model recognises + echoes *for the ear* only.

**Idempotency (A9-D-5): draft-hash-primary over the verbatim ask + conversation.** One confirmed
ask in one call → one delegated turn; a double-confirm / event redelivery converges (the over-dedup
bias is correct for a mishearable channel). The optional ``dedup_token`` is the **escape hatch**
for a deliberate same-phrase repeat (a gate-minted per-proposal token overrides the ask hash).
"""

from __future__ import annotations

import hashlib

from persona.jobs.models import JobPayload

__all__ = [
    "DELEGATED_TURN_JOB_TYPE",
    "PROVENANCE_VOICE",
    "DelegatedTurnPayload",
    "delegated_turn_idempotency_key",
    "make_delegated_turn_payload",
]

#: The durable job type string (the A0 ``jobs.type`` value + registry key).
DELEGATED_TURN_JOB_TYPE = "delegated_turn"

#: The provenance marker recorded on the delegation (the audit row's ``actor``/origin). Voice is the
#: only originator today; the field exists so a delegated turn is attributable to the call it was
#: born on (A9-D-7 — ``provenance=voice`` on the audit row).
PROVENANCE_VOICE = "voice"


class DelegatedTurnPayload(JobPayload):
    """The confirmed spoken ask to execute as a chat turn (frozen + ``extra='forbid'``).

    Attributes:
        conversation_id: The call's conversation — the delegated chat turn runs in it (the same
            conversation the voice turns persist to), so the frontier sees the full history.
        verbatim_ask: The verbatim spoken ask (original + any amendment utterances). **Never a
            parsed draft** — the frontier re-parses it authoritatively (A9-D-5).
        persona_id: The persona on the call (the delegated turn is the SAME persona).
        provenance: The originating surface (``voice``) — recorded on the audit row (A9-D-7).
        dedup_token: The A9-D-5 escape hatch. ``None`` (default) ⇒ the idempotency key hashes the
            verbatim ask; a gate-minted per-proposal token overrides it for a deliberate same-phrase
            repeat. It does not enter the *payload* semantics, only the key.
    """

    conversation_id: str
    verbatim_ask: str
    persona_id: str
    provenance: str = PROVENANCE_VOICE
    dedup_token: str | None = None


def delegated_turn_idempotency_key(payload: DelegatedTurnPayload) -> str:
    """``delegate:{conversation_id}:{anchor}`` — the dedup identity (A9-D-5).

    ``anchor`` is the ``dedup_token`` when set (the escape hatch), else the SHA-256 of the verbatim
    ask (draft-hash-primary over the ask). Scoped to the conversation, so the same ask in two
    different calls is two distinct delegations, and a double-confirm within one call converges.
    Owner scoping is the ``jobs`` unique index's ``owner_id`` half, not the key.
    """
    token = (payload.dedup_token or "").strip()
    anchor = token or hashlib.sha256(payload.verbatim_ask.encode("utf-8")).hexdigest()
    return f"delegate:{payload.conversation_id}:{anchor}"


def make_delegated_turn_payload(
    *,
    conversation_id: str,
    verbatim_ask: str,
    persona_id: str,
    provenance: str = PROVENANCE_VOICE,
    dedup_token: str | None = None,
) -> DelegatedTurnPayload:
    """Build the payload for a confirmed spoken delegation (the one ``intent → payload`` mapping).

    Both writers — the voice raw-INSERT and any api-side re-enqueue — call this, so they produce
    byte-identical payloads + keys and cannot drift.
    """
    return DelegatedTurnPayload(
        conversation_id=conversation_id,
        verbatim_ask=verbatim_ask,
        persona_id=persona_id,
        provenance=provenance,
        dedup_token=dedup_token,
    )
