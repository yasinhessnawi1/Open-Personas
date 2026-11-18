"""The direct-write capability — ``record_user_fact`` (Spec K2, T7; D-K2-1).

A Spec-03 ``AsyncTool`` available to all personas by default: the persona invokes
it mid-interaction to record an explicit, discrete, durable fact about the user
(the way a good assistant jots a note). The persona's in-context judgement IS the
extraction — so the tool does NO LLM work; it builds one ``KnowledgeCandidate``
and runs ONE inline ``GraphStore.merge`` (canonicalise → extend-vs-create → link →
index-sync). A single merge is tens of ms, so the write is fire-and-forget from
the turn's perspective — it never blocks or slows the reply (criterion 10). The
immediacy is the point (the next persona, minutes later, should know the fact);
enqueuing would reintroduce the gap the direct path exists to close.

Grounding rides for free: the persona is asserting the user conveyed the fact, so
the fact itself is the provenance grounding (``source = persona_self``). Entity
threading and the emergent understanding are synthesis's job (T8) — the direct
write is for the discrete fact, now.

The ``@tool`` decorator's envelope turns any body exception into
``ToolResult(is_error=True)`` (D-03-5), so a merge failure surfaces as an error
result and never crashes the live turn.

R9-253: the write names the interaction its turn is bound to
(:mod:`persona_runtime.interaction_scope`), and a disclosure (a category, or self-harm
means that get the write rejected) records the durable disclosure fact BEFORE anything is
written, so initiative's wellbeing gate holds that interaction even after the user forgets
the node. A fact that cannot be recorded refuses the write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph.models import NodeKind, NodeProvenance
from persona.graph.protocol import KnowledgeCandidate
from persona.logging import get_logger
from persona.schema.chunks import WriteSource
from persona.schema.tools import ToolResult
from persona.tools.protocol import tool
from persona.wellbeing import WellbeingCategory

from persona_runtime.extraction.disclosure import is_disclosure
from persona_runtime.extraction.means_guard import contains_self_harm_means

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.protocol import GraphStore
    from persona.tools.protocol import AsyncTool

    from persona_runtime.extraction.disclosure import DisclosureRecorder
    from persona_runtime.interaction_scope import InteractionRef

__all__ = ["make_record_user_fact_tool"]

_logger = get_logger("extraction.direct_write")

_CONCEPT_NAME_WORDS = 8
_CONCEPT_NAME_CHARS = 64

# The behavioural surface (K2-R-3) — the model reads this as the tool description.
_GUIDANCE = """\
Record a single explicit, durable fact the user stated about themselves — a \
preference, an allergy, a relationship, a job change, a goal. Record it ONCE, \
SILENTLY, in the user's own words. Do not narrate that you are saving it. Do not \
record passing task mechanics, transient state, or anything you inferred rather \
than were told. For a sensitive disclosure set ``category``; never record \
self-harm methods or means — record only the care-relevant fact.

Args:
    fact: The durable fact, in the user's own framing.
    category: For a sensitive disclosure, one of self_harm, disordered_eating, \
mental_health_crisis, abuse_victimisation, substance_addiction; otherwise omit.
"""


def _concept_name(fact: str) -> str:
    """Derive a short concept label from the fact (no LLM — keep it fast)."""
    label = " ".join(fact.split()[:_CONCEPT_NAME_WORDS])
    return label[:_CONCEPT_NAME_CHARS]


def _coerce_category(category: str | None) -> WellbeingCategory | None:
    """Map a category string to the enum; an unknown value is dropped (untagged)."""
    if category is None:
        return None
    try:
        return WellbeingCategory(category)
    except ValueError:
        _logger.warning("unknown direct-write category dropped to untagged", category=category)
        return None


_NOT_HELD = "Not recorded: this could not be saved safely just now. Do not retry it in this turn."


def _fact_refusal(
    disclosures: DisclosureRecorder | None,
    interaction: InteractionRef | None,
    *,
    owner_id: str,
    persona_id: str | None,
) -> ToolResult | None:
    """Record the disclosure fact first (R9-253); an error result when it is not held.

    No recorder, no persona or no bound interaction: nothing to key the fact by, so the write
    goes on as before (the node is tagged, and gated by that alone). Logs carry ids only.
    """
    if disclosures is None or not persona_id:
        return None
    if interaction is None:
        _logger.warning(
            "direct write disclosure with no bound interaction: no fact", persona_id=persona_id
        )
        return None
    try:
        held = disclosures.record(owner_id, persona_id, interaction.id)
    except Exception as exc:  # noqa: BLE001 - refused below; the turn must not crash
        # The class only: a driver's message can carry the statement's parameters.
        _logger.warning(
            "direct write refused: the disclosure fact failed",
            persona_id=persona_id,
            interaction_id=interaction.id,
            error_class=type(exc).__name__,
        )
        held = False
    if held:
        return None
    return ToolResult(tool_name="record_user_fact", content=_NOT_HELD, is_error=True)


def make_record_user_fact_tool(
    *,
    graph_store: GraphStore,
    owner_provider: Callable[[], str | None],
    persona_id: str | None = None,
    persona_exists: Callable[[str], bool] | None = None,
    interaction_provider: Callable[[], InteractionRef | None] | None = None,
    disclosures: DisclosureRecorder | None = None,
) -> AsyncTool:
    """Build the ``record_user_fact`` tool bound to a graph store + owner context.

    Args:
        graph_store: The user-scoped graph store the write merges into.
        owner_provider: Resolves the current owner (user) id at DISPATCH time
            (the toolbox is built once; the owner is per-request). ``None`` ⇒ the
            write fails closed.
        persona_id: The persona recording the fact (→ provenance).
        persona_exists: Asked at dispatch whether ``persona_id`` still exists (R9-284). The
            graph is owner scoped, so nothing else refuses a write from a turn that was still
            running when its persona was erased; without this it would put the persona back
            into a graph the erasure took it out of. ``None`` skips the check.
        interaction_provider: Resolves the turn's bound interaction at DISPATCH time
            (R9-253); the write's provenance names it. ``None`` names none.
        disclosures: Records the durable disclosure fact before a tagged or means-rejected
            write (R9-253). ``None`` records nothing.
    """

    @tool(name="record_user_fact", description=_GUIDANCE)
    async def record_user_fact(fact: str, category: str | None = None) -> ToolResult:
        owner_id = owner_provider()
        if not owner_id:
            return ToolResult(
                tool_name="record_user_fact",
                content="No user context for the write.",
                is_error=True,
            )
        if not fact.strip():
            return ToolResult(
                tool_name="record_user_fact",
                content="Nothing to record: the fact is empty.",
                is_error=True,
            )
        if persona_id and persona_exists is not None and not persona_exists(persona_id):
            return ToolResult(
                tool_name="record_user_fact",
                content="Not recorded: this persona has been deleted.",
                is_error=True,
            )
        wellbeing = _coerce_category(category)
        interaction = interaction_provider() if interaction_provider is not None else None
        if is_disclosure(fact, wellbeing.value if wellbeing is not None else None):
            refusal = _fact_refusal(
                disclosures, interaction, owner_id=owner_id, persona_id=persona_id
            )
            if refusal is not None:
                return refusal
        # Structural means backstop (D-K2-7): self-harm method/means must never
        # enter the graph. Best-effort deterministic scan — reject the write
        # (record the struggle without the means) rather than store specifics.
        if contains_self_harm_means(fact, category=wellbeing.value if wellbeing else None):
            _logger.warning(
                "direct write rejected: self-harm means specifics", persona_id=persona_id
            )
            return ToolResult(
                tool_name="record_user_fact",
                content=(
                    "Not recorded: record only the care-relevant fact, never the "
                    "method or means. The specifics must not be stored."
                ),
                is_error=True,
            )
        candidate = KnowledgeCandidate(
            concept_name=_concept_name(fact),
            content=fact,
            node_kind=NodeKind.FACT,
            wellbeing_category=wellbeing.value if wellbeing is not None else None,
            provenance=NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                persona_id=persona_id,
                interaction_id=interaction.id if interaction is not None else None,
                interaction_kind=interaction.kind if interaction is not None else None,
                written_at=datetime.now(UTC),
                grounding=fact,
                reason="direct write",
            ),
        )
        # One fast inline merge — the body raising is enveloped to is_error by the
        # decorator (D-03-5), so a write failure never crashes the live turn.
        outcome = graph_store.merge(owner_id, candidate)
        return ToolResult(
            tool_name="record_user_fact",
            content=f"Noted. (graph node {outcome.node_id})",
        )

    return record_user_fact
