"""The research skill's cross-leg rules reach the model (Spec W1, T11; D-W1-12).

The skill is longer than the 2000-token injection budget, so the injector truncates it: only
the opening survives into the prompt. That makes POSITION a correctness property here, not a
matter of taste. Rules added at the bottom of a 3000-token file are rules nobody reads.

These pin that the four leg rules exist, that they sit inside the budget that actually
reaches a model, and that the frontmatter still parses after the edit.
"""

from __future__ import annotations

from persona.skills import BUILTIN_ROOT, SkillInjector, SkillScanner, count_tokens

_RULES = (
    "Batch",
    "Never repeat a failed call",
    "QUERIES ALREADY RUN",
    "Deliver partially, early",
)


def _spec() -> object:
    [spec] = SkillScanner([BUILTIN_ROOT]).scan(["web_research"])
    return spec


def test_the_frontmatter_still_parses() -> None:
    spec = _spec()
    assert spec.name == "web_research"  # type: ignore[attr-defined]
    assert "web_search" in spec.tools_required  # type: ignore[attr-defined]
    assert spec.when_to_use  # type: ignore[attr-defined]


def test_the_cross_leg_rules_are_in_the_skill() -> None:
    content = _spec().content  # type: ignore[attr-defined]
    for rule in _RULES:
        assert rule in content, rule


def test_the_rules_survive_the_injection_budget() -> None:
    """The test that matters. The skill is over budget, so the injector truncates; a rule
    past the cut is a rule the model never sees."""
    content = _spec().content  # type: ignore[attr-defined]
    head = content[: _chars_within_budget(content, SkillInjector.TOKEN_BUDGET)]

    for rule in _RULES:
        assert rule in head, f"{rule} falls outside the {SkillInjector.TOKEN_BUDGET}-token budget"


def test_the_rules_come_before_the_procedure() -> None:
    """Ordering is the mechanism: the procedure is what a leg does, the rules are how it
    does it across legs, and only the opening of this file is guaranteed to be read."""
    content = _spec().content  # type: ignore[attr-defined]
    assert content.index("Working across legs") < content.index("## Procedure")


def _chars_within_budget(content: str, budget: int) -> int:
    """The character count whose token count is still inside ``budget`` (bisection)."""
    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(content[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo
