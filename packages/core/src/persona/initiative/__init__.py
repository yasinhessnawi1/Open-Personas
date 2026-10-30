"""Initiative — the persona that notices, decides, and acts unprompted (Spec A5).

persona-core owns the pure contracts and policy: the candidate entry contract
(the A7-consumable seam), the closed trigger catalogue, the dial, and the
env-driven restraint settings. Restraint is the product: everything here is
shaped so a candidate must EARN delivery through grounding, envelope, and
restraint gates — silence is the safe state.
"""

from persona.initiative.arbitration import arbitrate_voicer
from persona.initiative.config import InitiativeSettings
from persona.initiative.dial import DEFAULT_INITIATIVE_DIAL, InitiativeDial
from persona.initiative.models import (
    INITIATIVE_TRIGGER_SET_VERSION,
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeTrigger,
    PlannedStep,
    ScheduleChange,
    Urgency,
    binds_across_triggers,
    opportunity_anchor,
)
from persona.initiative.versions import (
    MAX_CITED_NODE_READS,
    MIN_DELIVERABLE_PROMPT_VERSIONS,
    prompt_version_deliverable,
)
from persona.initiative.voice import address_user

__all__ = [
    "DEFAULT_INITIATIVE_DIAL",
    "INITIATIVE_TRIGGER_SET_VERSION",
    "MAX_CITED_NODE_READS",
    "MIN_DELIVERABLE_PROMPT_VERSIONS",
    "CandidateSource",
    "CitationKind",
    "GroundingCitation",
    "InitiativeCandidate",
    "InitiativeDial",
    "InitiativeSettings",
    "InitiativeTrigger",
    "PlannedStep",
    "ScheduleChange",
    "Urgency",
    "address_user",
    "arbitrate_voicer",
    "binds_across_triggers",
    "opportunity_anchor",
    "prompt_version_deliverable",
]
