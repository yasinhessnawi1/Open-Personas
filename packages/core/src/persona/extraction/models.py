"""The pre-resolution extraction boundary shapes (Spec K2, T1; D-K2-4).

K0 already owns the *post*-resolution write contract (``KnowledgeCandidate`` in
:mod:`persona.graph.protocol`) — the single shape ``GraphStore.merge`` consumes.
This module owns the *pre*-resolution shapes the runtime extractor produces, which
the K2 orchestrator then maps into ``KnowledgeCandidate``:

- entity *mentions* (raw surface forms) → resolved ``entity_ids`` (via K0's
  ``EntityRegistry.resolve`` + K2's AMBIGUOUS-band judge);
- the verbatim ``evidence_span`` → ``NodeProvenance.grounding``.

Keeping these in persona-core (frozen, ``extra="forbid"``, the boundary-type
convention) lets the runtime extractor depend on a typed contract while the LLM
that fills it stays in runtime (the ratified layering split; core is LLM-free).

**The grounding invariant is structural here:** ``ExtractionCandidate`` requires a
non-empty ``evidence_span``. A candidate with no quotable basis cannot be
constructed — the construction-time half of the grounded-extraction safety bar
(criterion 5); the measured half is the K2-R-2 evaluation (T6).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from persona.graph.models import (  # noqa: TC001 — Pydantic needs runtime access
    LinkType,
    NodeKind,
)
from persona.graph.protocol import UpdateIntent  # noqa: TC001 — Pydantic needs runtime access
from persona.logging import get_logger
from persona.wellbeing import WellbeingCategory  # noqa: TC001 — Pydantic needs runtime access

__all__ = [
    "EntityMention",
    "ExtractionCandidate",
    "ExtractionInput",
    "InteractionKind",
    "ProposedRelation",
]

_logger = get_logger("extraction.models")

# The only link types K2 ASSERTS at extraction (D-K0-8): temporal where the account
# orders events, causal only on stated/strongly-implied causation. SEMANTIC is K0's
# automatic baseline; ENTITY falls out of canonical resolution — neither is asserted.
_K2_ASSERTABLE_LINKS = frozenset({LinkType.TEMPORAL, LinkType.CAUSAL})

# The graph-node label guide (K12, T5; D-K12-D): concept_name is a concise 1-3
# word noun phrase — the prompt (persona_runtime.extraction.prompt) instructs the
# model directly. D-K12-D is explicit that this is a *soft* length guide, NOT a
# hard truncation: a meaningful phrase beats a mangled word-count cut, so this
# module never rewrites the model's label — it only normalises surrounding
# whitespace.


class InteractionKind(StrEnum):
    """Which kind of completed interaction synthesis is reading (K2 §2).

    The channel-agnostic discriminator (the C1 forward seam): a web chat, a
    completed agentic run (Spec 06 metadata), and a voice conversation all
    synthesise the same way. The values match the ``synthesis_markers``
    ``interaction_kind`` CHECK constraint (D-K2-X-migration-placeholder), so the
    extraction input and the idempotency marker share one vocabulary.
    """

    CONVERSATION = "conversation"
    AGENTIC_RUN = "agentic_run"
    VOICE = "voice"


class EntityMention(BaseModel):
    """A raw entity surface form recognised in an interaction (pre-resolution).

    The extractor records *what was said* ("my doctor", "Dr. Hansen"); resolution
    to a canonical entity id is the orchestrator's job (K0's ``EntityRegistry`` +
    K2's AMBIGUOUS-band judge, D-K0-9). Enriched with resolution hints (kind,
    offsets) in later tasks if needed — additive.

    Attributes:
        surface: The mention text as it appeared in the interaction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str = Field(min_length=1)


class ProposedRelation(BaseModel):
    """A typed relationship K2 asserts between this candidate and another concept.

    The pre-resolution analogue of K0's ``ProposedLink``: ``target_concept`` is a
    *name* reference (resolved to a ``target_node_id`` once the batch is merged),
    not yet a node id. K2 asserts ONLY ``TEMPORAL`` (where the account orders
    events) and ``CAUSAL`` (only on stated/strongly-implied causation, D-K0-8);
    ``SEMANTIC`` (K0's automatic baseline) and ``ENTITY`` (from canonical
    resolution) are rejected at the boundary — a wrong "because" about someone's
    life is worse than no link.

    Attributes:
        target_concept: The related concept's name (resolved to a node id later).
        link_type: ``TEMPORAL`` or ``CAUSAL`` only.
        reason: The grounding for the relation (the stated ordering/causation).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_concept: str = Field(min_length=1)
    link_type: LinkType
    reason: str | None = None

    @model_validator(mode="after")
    def _only_temporal_or_causal(self) -> ProposedRelation:
        if self.link_type not in _K2_ASSERTABLE_LINKS:
            msg = (
                f"K2 asserts only temporal/causal links; got {self.link_type.value!r} "
                "(semantic is K0's automatic baseline, entity falls out of resolution)"
            )
            raise ValueError(msg)
        return self


class ExtractionCandidate(BaseModel):
    """One grounded candidate the extractor proposes (pre-resolution; K2 §2/§4).

    Maps to K0's ``KnowledgeCandidate`` after the orchestrator resolves
    ``entity_mentions`` → ``entity_ids`` and folds ``evidence_span`` into
    ``NodeProvenance.grounding``. Frozen + ``extra="forbid"`` (the boundary-type
    convention).

    Attributes:
        concept_name: Short canonical label for the concept — the graph's node
            label (K12, T5). A concise 1-3 word noun phrase; the model is
            instructed to produce this directly (the prompt). This is a soft
            length guide, not a hard truncation (D-K12-D): a label that runs
            long is kept intact — a meaningful phrase beats a mangled cut.
            This field only strips surrounding whitespace.
        content: The durable understanding, in the user's framing.
        node_kind: What the candidate represents (:class:`persona.graph.models.NodeKind`).
        evidence_span: The **verbatim** basis from the interaction that grounds
            this candidate. Required and non-empty — the structural grounding
            invariant (criterion 5). Lands in ``NodeProvenance.grounding``.
        entity_mentions: Raw entity surface forms this concept concerns
            (resolved to ``entity_ids`` downstream).
        wellbeing_category: The sensitive-disclosure tag set at extraction time,
            only on a clear user disclosure (criterion 7; D-K2-X-wellbeing-category-set).
            ``None`` for the overwhelming majority of candidates.
        update_intent: Whether this updates/contradicts prior knowledge
            (:class:`persona.graph.protocol.UpdateIntent`); the orchestrator resolves
            the target node (D-K0-4, no silent overwrite).
        update_target_hint: A free-text description of *what* prior knowledge this
            updates (e.g. "works at X"), resolved to a ``target_node_id``
            downstream. ``None`` when ``update_intent`` is ``NONE``.
        proposed_relations: Typed (temporal/causal) relationships this candidate
            asserts to other concepts (:class:`ProposedRelation`); each
            ``target_concept`` is resolved to a node id at batch-merge time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    concept_name: str = Field(min_length=1)
    content: str = Field(min_length=1)
    node_kind: NodeKind
    evidence_span: str = Field(min_length=1)
    entity_mentions: tuple[EntityMention, ...] = ()
    wellbeing_category: WellbeingCategory | None = None
    update_intent: UpdateIntent = UpdateIntent.NONE
    update_target_hint: str | None = None
    proposed_relations: tuple[ProposedRelation, ...] = ()

    @field_validator("concept_name", mode="after")
    @classmethod
    def _normalize_concept_name_whitespace(cls, value: str) -> str:
        """Soft length guide (K12, T5; D-K12-D): never truncate, only normalise.

        The prompt instructs a 1-3 word ``concept_name``; that's the actual guide.
        This validator does NOT enforce the word count — a model that emits a
        longer-but-meaningful label is kept as-is, since a hard word-count cut
        can mangle meaning (e.g. "focus during long study sessions" truncated to
        "focus during long" drops the actual concept). Only surrounding
        whitespace is stripped. If stripping would empty the value, the original
        is kept so the ``min_length=1`` contract still holds (the prompt
        guarantees non-empty output; this validator never raises).
        """
        stripped = value.strip()
        if not stripped:
            return value
        if stripped != value:
            _logger.debug(
                "concept_name whitespace normalized",
                original=value,
                normalized=stripped,
            )
        return stripped


class ExtractionInput(BaseModel):
    """The interaction handed to the extractor, plus its provenance material (K2 §2).

    Carries the (windowed, D-K2-5) text the extractor reads and the identifiers
    that become the candidate's provenance (``persona_id`` / ``interaction_id``)
    and drive the synthesis idempotency marker (``interaction_kind`` /
    ``interaction_id``).

    Attributes:
        interaction_kind: The channel (:class:`InteractionKind`).
        interaction_id: The source interaction's id (conversation / run / voice session).
        persona_id: The persona whose interaction this is (→ ``NodeProvenance.persona_id``).
        content: The interaction text to extract from (already windowed if long).
        channel: The originating surface (``chat`` / ``voice``, Spec V13) → the
            minted node's ``NodeProvenance.channel``. Defaults to ``chat`` so
            existing callers are unchanged; voice synthesis sets ``voice``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    interaction_kind: InteractionKind
    interaction_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)
    content: str
    channel: str = "chat"
