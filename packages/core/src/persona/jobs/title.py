"""The title-refresh job contract — the ONE definition both writers share (R9-020/R9-028).

The durable ``title_refresh`` job is enqueued from two processes: persona-api (a
chat turn-end threshold crossing, via ``JobQueue.enqueue``) and persona-voice (a
call session-end, via a small raw INSERT beside its other raw-SQL peers — the
``synthesis`` (V13 D-4-amended) precedent this module mirrors exactly). Keeping
the *table* api-owned but collapsing the **semantic** drift surface to a single
definition here in core means the two thin writers cannot disagree about *what*
a title-refresh job is, only about *how* they INSERT the row.

``persona.jobs`` already owns the job domain (``JobPayload``) and this is domain,
not persistence: no SQL, no table knowledge.
"""

from __future__ import annotations

from persona.jobs.models import JobPayload

__all__ = [
    "TITLE_REFRESH_JOB_TYPE",
    "TitleRefreshJobPayload",
    "title_refresh_idempotency_key",
]

#: The durable job type string (the A0 ``jobs.type`` value + registry key).
TITLE_REFRESH_JOB_TYPE = "title_refresh"


class TitleRefreshJobPayload(JobPayload):
    """Which conversation to re-title, keyed by the threshold that fired it.

    ``threshold`` is a dedup/idempotency scope, not restricted to the web
    trigger's fixed crossings (:data:`persona_api.services.title_trigger.
    TITLE_REFRESH_THRESHOLDS`) — the voice call session-end writer keys it on
    the call's FINAL message count instead, so a re-run of teardown is a safe
    ``ON CONFLICT`` no-op rather than a duplicate regen. The handler always
    re-reads the WHOLE live transcript regardless of which value fired it.
    """

    conversation_id: str
    threshold: int


def title_refresh_idempotency_key(payload: TitleRefreshJobPayload) -> str:
    """``title:{conversation_id}:{threshold}`` — one refresh per threshold crossing."""
    return f"title:{payload.conversation_id}:{payload.threshold}"
