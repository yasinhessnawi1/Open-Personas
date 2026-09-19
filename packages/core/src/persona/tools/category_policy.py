"""The per-task category policy matrix — allow / gate / deny over the conservative defaults
(A3-D-1, A3-D-X-policy-split).

Where :mod:`persona.tools.categories` declares *what kind of effect* a tool has, this module
decides *whether a given task may run it unattended*: each :class:`ActionCategory` resolves to
a :class:`CategoryDecision` (``allow`` / ``gate`` / ``deny``). The matrix is **sparse** — it
stores only the per-task *overrides*; everything else falls to the conservative default (free
categories → ``allow``, gated-by-default categories → ``gate``). So:

- an **unconfigured** task (``DEFAULT_POLICY``, no overrides) observes/computes/drafts/notifies
  freely and **gates** the four consequential categories (criterion 2);
- a contract clause (A4) **loosens** one category ("yes, it may ``spend`` up to $1500 without
  asking" → ``spend: allow``) or **tightens** it (``external_mutate: deny``) without touching
  the rest — granted once, visibly, where the user is paying attention (the grant-at-contract,
  gate-at-the-exception discipline);
- a category added to the taxonomy later inherits its conservative default for free (the
  matrix never has to be exhaustively re-declared).

A tool spans one or more categories; :meth:`CategoryPolicy.decide_tool` takes the
**most-restrictive** decision across them (``deny`` > ``gate`` > ``allow``) — so a
network-enabled ``code_execution`` (``{compute, external_mutate}``) gates even though
``compute`` alone would run. The matrix is carried on the A2 :class:`persona.tasks.Contract`
(A4 authors it; A3 enforces it at the dispatch boundary).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from persona.tools.categories import FREE_CATEGORIES, ActionCategory

__all__ = [
    "DEFAULT_POLICY",
    "CategoryDecision",
    "CategoryPolicy",
    "CategoryRule",
    "default_decision",
]


class CategoryDecision(StrEnum):
    """Whether a task may run a category unattended.

    ``allow`` — run it without asking. ``gate`` — pause and ask (the approval flow).
    ``deny`` — refuse it unattended entirely (the model adapts within the leg).
    """

    ALLOW = "allow"
    GATE = "gate"
    DENY = "deny"


#: Severity order for the most-restrictive-wins rule (deny dominates gate dominates allow).
_SEVERITY: dict[CategoryDecision, int] = {
    CategoryDecision.ALLOW: 0,
    CategoryDecision.GATE: 1,
    CategoryDecision.DENY: 2,
}


def default_decision(category: ActionCategory) -> CategoryDecision:
    """The conservative default for ``category``: free → ``allow``, gated-by-default → ``gate``."""
    return CategoryDecision.ALLOW if category in FREE_CATEGORIES else CategoryDecision.GATE


class CategoryRule(BaseModel):
    """One per-task override: a category and the decision that replaces its default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: ActionCategory
    decision: CategoryDecision


class CategoryPolicy(BaseModel):
    """The per-task allow/gate/deny matrix, stored as sparse overrides over the defaults.

    Frozen and immutable (a leg cannot rewrite it; it travels on the frozen
    :class:`persona.tasks.Contract`). The empty-overrides instance is :data:`DEFAULT_POLICY`
    — the conservative seed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overrides: tuple[CategoryRule, ...] = ()

    @model_validator(mode="after")
    def _no_duplicate_categories(self) -> CategoryPolicy:
        seen = {rule.category for rule in self.overrides}
        if len(seen) != len(self.overrides):
            msg = "a category may appear at most once in a CategoryPolicy's overrides"
            raise ValueError(msg)
        return self

    def decide(self, category: ActionCategory) -> CategoryDecision:
        """The decision for ``category``: its override if set, else the conservative default."""
        for rule in self.overrides:
            if rule.category == category:
                return rule.decision
        return default_decision(category)

    def decide_tool(self, categories: frozenset[ActionCategory]) -> CategoryDecision:
        """The decision for a tool spanning ``categories`` — the most restrictive across them.

        Empty ``categories`` gates defensively (it should never happen —
        :func:`persona.tools.categories.resolve_action_categories` is total and non-empty —
        but a tool with no resolvable effect must not run unattended for free).
        """
        if not categories:
            return CategoryDecision.GATE
        return max((self.decide(c) for c in categories), key=lambda d: _SEVERITY[d])


#: The conservative seed: no overrides → free categories allow, gated-by-default categories
#: gate. An unconfigured task is bounded by this (criterion 2).
DEFAULT_POLICY = CategoryPolicy()
