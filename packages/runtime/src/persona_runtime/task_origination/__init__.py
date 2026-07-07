"""Task origination — the conversational contract flow (Spec A4).

A chat turn carrying *standing intent* ("every morning…", "keep watching…") opens the
**contract path**: the persona drafts a contract (this package's :class:`ContractDraft`),
echoes it back compactly, accepts adjust-by-reply, and on one explicit confirmation the
api creates the A2 task + A1 schedule + A3 matrix. Recognition, drafting, and the echo
live here in ``persona-runtime`` (which cannot import ``persona-api``) — so a leg and a
chat turn compose the identical runtime, the no-bypass invariant made structural.

Public surface:

- :class:`ContractDraft` — the in-flight draft assembled from the conversation (the unit
  echoed, adjusted, and finally confirmed-and-created).
- :class:`GrantSpec` / :class:`ParsedSchedule` — the draft's beyond-default permissions and
  its parsed (A1-shaped) cadence.
- :func:`build_contract` — assemble a frozen :class:`persona.tasks.Contract` from a draft.
- :func:`parse_recurrence` / :func:`parse_one_time` / :func:`render_human_terms` — schedule
  parsing with the parse-honesty boundary (T2).
- :func:`render_echo` / :func:`render_clause` + the ``amend_*`` adjust-by-reply surface and
  the versioned echo prompt artifact (T3).
"""

from __future__ import annotations

from persona_runtime.task_origination.amendment import (
    AmendmentInterpreter,
    changed_clauses,
    classify_amendment_materiality,
)
from persona_runtime.task_origination.amendment_model import (
    AMENDMENT_PROMPT_VERSION,
    ModelAmendmentInterpreter,
)
from persona_runtime.task_origination.confirm import is_affirmative_confirmation
from persona_runtime.task_origination.cues import CueSignal, detect_standing_cue
from persona_runtime.task_origination.draft import (
    ContractDraft,
    GrantSpec,
    ParsedSchedule,
    build_contract,
    canonicalize_draft,
)
from persona_runtime.task_origination.echo import (
    ECHO_PROMPT,
    ECHO_PROMPT_VERSION,
    ECHO_PROMPT_VOICE,
    ECHO_PROMPT_VOICE_VERSION,
    Clause,
    EchoMode,
    amend_goal,
    amend_schedule,
    amend_scope,
    amend_updates,
    clear_grant,
    render_clause,
    render_echo,
    set_grant,
)
from persona_runtime.task_origination.emission import (
    build_task_originated_event,
    draft_content_hash,
)
from persona_runtime.task_origination.judge import (
    JUDGE_PROMPT_VERSION,
    ModelStandingIntentJudge,
)
from persona_runtime.task_origination.recognizer import (
    RecognitionKind,
    RecognitionOutcome,
    StandingIntentJudge,
    StandingIntentRecognizer,
    StandingJudgment,
    StandingVerdict,
    build_clarify_question,
)
from persona_runtime.task_origination.reschedule import (
    RescheduleIntent,
    RescheduleInterpreter,
    RescheduleResolution,
    RescheduleResolutionKind,
    detect_reschedule_cue,
    render_proposal_echo,
    render_reschedule_echo,
)
from persona_runtime.task_origination.reschedule_model import (
    RESCHEDULE_PROMPT_VERSION,
    ModelRescheduleInterpreter,
)
from persona_runtime.task_origination.schedule import (
    parse_one_time,
    parse_recurrence,
    render_human_terms,
)
from persona_runtime.task_origination.steering import (
    SteeringIntent,
    SteeringInterpreter,
    SteeringVerb,
    detect_steering_cue,
)
from persona_runtime.task_origination.steering_model import (
    STEERING_PROMPT_VERSION,
    ModelSteeringInterpreter,
)

__all__ = [
    "AMENDMENT_PROMPT_VERSION",
    "ECHO_PROMPT",
    "ECHO_PROMPT_VERSION",
    "ECHO_PROMPT_VOICE",
    "ECHO_PROMPT_VOICE_VERSION",
    "JUDGE_PROMPT_VERSION",
    "RESCHEDULE_PROMPT_VERSION",
    "STEERING_PROMPT_VERSION",
    "AmendmentInterpreter",
    "Clause",
    "ModelAmendmentInterpreter",
    "ModelRescheduleInterpreter",
    "ModelStandingIntentJudge",
    "ModelSteeringInterpreter",
    "RescheduleIntent",
    "RescheduleInterpreter",
    "RescheduleResolution",
    "RescheduleResolutionKind",
    "detect_reschedule_cue",
    "render_proposal_echo",
    "render_reschedule_echo",
    "ContractDraft",
    "CueSignal",
    "EchoMode",
    "GrantSpec",
    "ParsedSchedule",
    "RecognitionKind",
    "RecognitionOutcome",
    "StandingIntentJudge",
    "StandingIntentRecognizer",
    "StandingJudgment",
    "StandingVerdict",
    "SteeringIntent",
    "SteeringInterpreter",
    "SteeringVerb",
    "amend_goal",
    "amend_schedule",
    "amend_scope",
    "amend_updates",
    "build_clarify_question",
    "build_contract",
    "build_task_originated_event",
    "canonicalize_draft",
    "changed_clauses",
    "classify_amendment_materiality",
    "clear_grant",
    "detect_standing_cue",
    "detect_steering_cue",
    "draft_content_hash",
    "is_affirmative_confirmation",
    "parse_one_time",
    "parse_recurrence",
    "render_clause",
    "render_echo",
    "render_human_terms",
    "set_grant",
]
