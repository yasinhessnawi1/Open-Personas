"""persona.jobs — the durable job contract (Spec A0, durable execution).

persona-core owns the job model + state machine + typed handler registry; the
durable Postgres queue (persona-api) and the worker service compose them. The
worker is a different *place* to run, never a different *thing* — these frozen
contracts are the shared truth that keeps the two execution paths from drifting.

See ``docs/specs/phase3/spec_A0/`` for the spec, decisions, and research.
"""

from __future__ import annotations

from persona.jobs.delegation import (
    DELEGATED_TURN_JOB_TYPE,
    PROVENANCE_VOICE,
    DelegatedTurnPayload,
    delegated_turn_idempotency_key,
    make_delegated_turn_payload,
)
from persona.jobs.models import (
    LONG_LEASE,
    MEDIUM_LEASE,
    SHORT_LEASE,
    TERMINAL_STATES,
    Job,
    JobPayload,
    JobState,
    LeasePolicy,
    RetryPolicy,
    can_transition,
    is_terminal,
    validate_transition,
)
from persona.jobs.registry import JobContext, JobHandler, JobRegistry, JobTypeSpec
from persona.jobs.synthesis import (
    CHANNEL_CHAT,
    CHANNEL_VOICE,
    SYNTHESIS_JOB_TYPE,
    SynthesisJobPayload,
    make_conversation_synthesis_payload,
    synthesis_idempotency_key,
)
from persona.jobs.title import (
    TITLE_REFRESH_JOB_TYPE,
    TitleRefreshJobPayload,
    title_refresh_idempotency_key,
)

__all__ = [
    "CHANNEL_CHAT",
    "CHANNEL_VOICE",
    "DELEGATED_TURN_JOB_TYPE",
    "LONG_LEASE",
    "MEDIUM_LEASE",
    "PROVENANCE_VOICE",
    "SHORT_LEASE",
    "SYNTHESIS_JOB_TYPE",
    "TERMINAL_STATES",
    "TITLE_REFRESH_JOB_TYPE",
    "DelegatedTurnPayload",
    "Job",
    "JobContext",
    "JobHandler",
    "JobPayload",
    "JobRegistry",
    "JobState",
    "JobTypeSpec",
    "LeasePolicy",
    "RetryPolicy",
    "SynthesisJobPayload",
    "TitleRefreshJobPayload",
    "can_transition",
    "delegated_turn_idempotency_key",
    "is_terminal",
    "make_conversation_synthesis_payload",
    "make_delegated_turn_payload",
    "synthesis_idempotency_key",
    "title_refresh_idempotency_key",
    "validate_transition",
]
