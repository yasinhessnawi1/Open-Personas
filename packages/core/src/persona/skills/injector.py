"""SkillInjector — enforces the 2000-token-per-turn skill content budget (T06).

When the runtime activates a skill (via the synthetic ``use_skill`` tool,
T07), the prompt builder calls :meth:`SkillInjector.inject` to get the
content to splice into the next turn's system prompt.

Three branches per D-04-7 + D-04-8:

1. ``skill.content_token_count <= TOKEN_BUDGET`` → return ``skill.content``
   verbatim. The common case for well-authored skills.
2. Over budget, a ``summariser`` was injected → ``await summariser(content)``.
   If the summary is still over budget (defensive — small-tier might be
   noisy), fall through to truncation.
3. Over budget, no summariser → token-aware truncation via binary search
   on character index; result ends with the ``MARKER`` literal.

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

from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.skills._tokens import count_tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.schema.skills import SkillSpec

__all__ = ["SkillInjector"]

_logger = get_logger("skills.injector")

# Marker appended to truncated content so the model knows the skill was
# cut short. Five tokens under cl100k_base — verified Phase 3 §5.
MARKER = "\n\n[truncated]"
_MARKER_TOKENS = count_tokens(MARKER)


class SkillInjector:
    """Returns skill content sized to fit the per-turn token budget.

    Constructor takes an optional ``summariser`` callable; the runtime
    adapts a small-tier :class:`persona.backends.ChatBackend` into this
    shape (spec 05's responsibility, not ours).

    Args:
        summariser: A callable that compresses long text into shorter text.
            Signature: ``Callable[[str], Awaitable[str]]``. When ``None``,
            over-budget content is truncated rather than summarised.
    """

    TOKEN_BUDGET: int = 2000  # non-negotiable per architecture §5.1.2 / D-04-7

    def __init__(
        self,
        *,
        summariser: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._summariser = summariser

    async def inject(self, skill: SkillSpec) -> str:
        """Return the content to inject for ``skill``, sized to the budget.

        Args:
            skill: The skill whose content should be injected. Its
                ``content_token_count`` is read directly (no re-tokenising)
                to decide the branch.

        Returns:
            A string that fits within the effective budget. Either: verbatim
            ``skill.content``, the summariser's output, or a character-prefix of
            ``skill.content`` followed by ``MARKER``.

        The effective budget is the skill's own ``token_budget`` when it
        declares one (Spec 24, D-24-5 per-skill override), else the class-wide
        :data:`TOKEN_BUDGET`. A per-skill override only tightens or loosens this
        one skill's content cap; it never changes the class default.
        """
        budget = skill.token_budget or self.TOKEN_BUDGET
        if skill.content_token_count <= budget:
            return skill.content

        if self._summariser is not None:
            summary = await self._summariser(skill.content)
            if count_tokens(summary) <= budget:
                return summary
            # Summariser exceeded the budget (rare; defensive). Fall
            # through to truncation on the SUMMARY, not the original — the
            # summariser at least made it shorter.
            _logger.warning(
                "summariser returned over-budget content; falling back to truncation",
                skill=skill.name,
                summary_tokens=count_tokens(summary),
                budget=budget,
            )
            return _truncate(summary, budget, skill=skill.name, source="summary")

        return _truncate(skill.content, budget, skill=skill.name, source="content")


def _truncate(content: str, budget: int, *, skill: str, source: str) -> str:
    """Largest prefix of ``content`` such that ``tokens(prefix + MARKER) <= budget``.

    Uses ceil-bisection on character index (D-04-8). O(log N) tokeniser
    calls.

    **Says so, at WARNING.** Until 2026-09-16 this cut skills silently: the only warning in
    the module guards the summariser overshoot, which cannot happen because no summariser is
    wired, so truncation — the behaviour that actually runs — emitted nothing at all. The
    built-in ``web_research`` skill has been losing 1,070 of its 3,069 tokens on every
    injection, and the lost tail is its entire source-evaluation rubric; an externally
    ingested skill on the owner's machine measured 17,627 tokens against a 2,000 budget.
    Neither was discoverable from a log. Both were found by a manual sweep, which is not a
    mechanism.

    This does not decide the POLICY (truncate / refuse / summarise is the owner's call, and
    the composition path already refuses rather than cuts for chained skills). It makes the
    current behaviour audible so the next one is found by reading a log.

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
        "prompt; shorten the skill or raise its token_budget.",
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
