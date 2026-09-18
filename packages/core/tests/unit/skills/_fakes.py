"""Small test fakes shared across the ``persona.skills`` test modules.

The injector reads cached summaries through the ``SkillSummaryLookup`` shape
(one ``get(content_hash, *, budget)`` call); a full
:class:`persona.skills.summary.SkillSummaryCache` is more than a unit test of
the injector's branching needs. This fake implements that shape directly and
records what it was asked.
"""

# ruff: noqa: ANN401, ARG001, ARG002

from __future__ import annotations


class FakeSummaryLookup:
    """A lookup that answers every hash with one fixed value (or a miss).

    ``calls`` records every ``(content_hash, budget)`` it was asked, so a test can
    assert the injector keyed on the right hash, or never asked at all.
    """

    def __init__(self, return_value: str | None = "summarised.") -> None:
        self.return_value = return_value
        self.calls: list[tuple[str, int]] = []

    def get(self, content_hash: str, *, budget: int) -> str | None:
        self.calls.append((content_hash, budget))
        return self.return_value
