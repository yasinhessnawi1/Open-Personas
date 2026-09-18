"""SkillInjector — enforces the 2000-token-per-turn skill content budget (T06).

When the runtime activates a skill (via the synthetic ``use_skill`` tool,
T07), the prompt builder calls :meth:`SkillInjector.inject` to get the
content to splice into the next turn's system prompt.

Three branches per D-04-7 + D-04-8, with the middle one reshaped by the
R9-165 ruling (2026-09-14):

1. ``skill.content_token_count <= budget`` → return ``skill.content``
   verbatim. The common case for well-authored skills.
2. Over budget, a ``summaries`` lookup was injected and it holds a summary
   for THIS body (keyed on the content hash) → return the cached summary.
   The lookup is a dictionary read: a skill is static content, so it is
   summarised once, at boot or at the mirror sync, never here. ``inject``
   never calls a model. A per-turn call would add model latency to every
   turn that uses a long skill and would inject a different body on
   different turns, which is worse than truncation.
3. Otherwise → token-aware truncation via binary search on character
   index; result ends with the ``MARKER`` literal, and the cut is logged at
   WARNING naming the skill and the overshoot.

The injector is **single-skill per call**. The "only one skill per turn"
rule (spec §7.1) is policy enforced by the runtime, not the injector — the
injector is stateless and has no concept of turns.

``TOKEN_BUDGET = 2000`` is a class constant, NOT env-overridable in v0.1
(D-04-7; architecture §5.1.2 "non-negotiable"). The runtime can pass a
different value at call time only via subclassing or direct constant
rebinding, both of which would be code changes, not configuration.

Token-aware truncation (D-04-8) uses **ceil-bisection** on character index;
``mid = (lo + hi + 1) // 2`` avoids the infinite loop at ``lo == hi - 1``
that floor-bisection produces.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

from persona.logging import get_logger
from persona.skills._tokens import count_tokens

if TYPE_CHECKING:
    from persona.schema.skills import SkillSpec

__all__ = ["SkillInjector", "SkillSummaryLookup", "content_hash_of", "skill_budget"]

_logger = get_logger("skills.injector")

# Marker appended to truncated content so the model knows the skill was
# cut short. Five tokens under cl100k_base — verified Phase 3 §5.
MARKER = "\n\n[truncated]"
_MARKER_TOKENS = count_tokens(MARKER)


class SkillSummaryLookup(Protocol):
    """A read-only view of cached skill summaries, keyed on the body's content hash.

    :class:`persona.skills.summary.SkillSummaryCache` is the production implementation.
    The injector only ever reads; producing a summary is the job of
    :func:`persona.skills.summary.ensure_skill_summaries`, run off the turn path.
    """

    def get(self, content_hash: str, *, budget: int) -> str | None:
        """The summary for ``content_hash`` if one exists and fits ``budget``, else ``None``."""
        ...


def content_hash_of(skill: SkillSpec) -> str:
    """sha256 of the body that would actually be injected: the summary cache key.

    Equals ``skill.provenance.content_hash`` for a freshly scanned or ingested skill. It is
    recomputed from ``skill.content`` rather than read from provenance because a step after
    the scan can rewrite the body without touching provenance (the document_generation
    fidelity resolution does), and a summary must never be served for a body it was not
    made from.
    """
    return hashlib.sha256(skill.content.encode("utf-8")).hexdigest()


class SkillInjector:
    """Returns skill content sized to fit the per-turn token budget.

    Args:
        summaries: Cached summaries keyed on content hash (see
            :class:`SkillSummaryLookup`). When ``None``, or when the lookup
            has no summary for the skill's current body, over-budget content
            is truncated.
    """

    TOKEN_BUDGET: int = 2000  # non-negotiable per architecture §5.1.2 / D-04-7

    def __init__(self, *, summaries: SkillSummaryLookup | None = None) -> None:
        self._summaries = summaries

    async def inject(self, skill: SkillSpec) -> str:
        """Return the content to inject for ``skill``, sized to the budget.

        Args:
            skill: The skill whose content should be injected. Its
                ``content_token_count`` is read directly (no re-tokenising)
                to decide the branch.

        Returns:
            A string that fits within the effective budget. Either: verbatim
            ``skill.content``, the cached summary of that exact body, or a
            character-prefix of ``skill.content`` followed by ``MARKER``.

        The effective budget is the skill's own ``token_budget`` when it
        declares one (Spec 24, D-24-5 per-skill override), else the class-wide
        :data:`TOKEN_BUDGET`. A per-skill override only tightens or loosens this
        one skill's content cap; it never changes the class default.

        Stays ``async`` for its callers' sake only; nothing in here awaits a
        model. Cached summaries are produced elsewhere (module docstring).
        """
        budget = skill_budget(skill)
        if skill.content_token_count <= budget:
            return skill.content

        if self._summaries is not None:
            summary = self._summaries.get(content_hash_of(skill), budget=budget)
            if summary is not None:
                if count_tokens(summary) <= budget:
                    _logger.debug(
                        "skill injected as its cached summary",
                        skill=skill.name,
                        tokens=skill.content_token_count,
                        budget=budget,
                    )
                    return summary
                # A lookup that hands back over-budget text is broken, not the skill.
                # Truncate the SUMMARY (it is at least shorter) and say so.
                _logger.warning(
                    "cached skill summary is over budget; falling back to truncation",
                    skill=skill.name,
                    summary_tokens=count_tokens(summary),
                    budget=budget,
                )
                return _truncate(summary, budget, skill=skill.name, source="summary")

        return _truncate(skill.content, budget, skill=skill.name, source="content")


def skill_budget(skill: SkillSpec) -> int:
    """The token budget this skill's injected content must fit (per-skill override or default)."""
    return skill.token_budget or SkillInjector.TOKEN_BUDGET


def _truncate(content: str, budget: int, *, skill: str, source: str) -> str:
    """Largest prefix of ``content`` such that ``tokens(prefix + MARKER) <= budget``.

    Uses ceil-bisection on character index (D-04-8). O(log N) tokeniser
    calls.

    **Says so, at WARNING.** Until 2026-09-16 this cut skills silently: the only warning in
    the module guarded a summariser overshoot that could not happen because no summariser
    was wired, so truncation — the behaviour that actually ran — emitted nothing at all. The
    built-in ``web_research`` skill had been losing 1,070 of its 3,069 tokens on every
    injection, and the lost tail is its entire source-evaluation rubric; an externally
    ingested skill on the owner's machine measured 17,627 tokens against a 2,000 budget.
    Neither was discoverable from a log. Both were found by a manual sweep, which is not a
    mechanism.

    Since R9-165 this is the FALLBACK: it runs only when no cached summary exists for the
    skill's current body (no small tier configured, the summary attempt failed, or the
    body changed and has not been re-summarised yet). The warning stays so that state is
    still found by reading a log.

    Edge cases:
    - Content already under budget → return verbatim (no marker; caller
      should already have checked, but this is defensive).
    - Budget smaller than the marker itself → return just the marker.
    """
    actual = count_tokens(content)
    if actual <= budget:
        return content
    _logger.warning(
        "skill {source} truncated to fit the turn budget — {lost} tokens dropped "
        "(skill={skill}, tokens={actual}, budget={budget}). The cut tail is gone from the "
        "prompt; shorten the skill, raise its token_budget, or let the summariser cache it.",
        skill=skill,
        source=source,
        actual=actual,
        budget=budget,
        lost=actual - budget,
    )
    target = budget - _MARKER_TOKENS
    if target <= 0:
        return MARKER
    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2  # ceil bisection; floor loops forever at lo == hi-1
        if count_tokens(content[:mid]) <= target:
            lo = mid
        else:
            hi = mid - 1
    return content[:lo] + MARKER
