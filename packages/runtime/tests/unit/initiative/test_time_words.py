"""``time_words``: what makes the flush re-ask the judge (fifth review LOW-4).

One row per family, so a family the function stops reading fails by name, plus the
words that must NOT read as time. The flush-level twin (a real checker in the
pipeline, judge called on unchanged grounds) lives in ``test_pipeline.py``.
"""

from __future__ import annotations

import pytest
from persona_runtime.initiative.specifics import relative_day_words, time_words


@pytest.mark.parametrize(
    ("family", "text", "word"),
    [
        ("relative phrase", "It renews tomorrow.", "tomorrow"),
        ("exempt possessive period", "This year's renewal is due.", "This year"),
        ("exempt day or two", "It renews in a day or two.", "in a day"),
        ("weekday", "It renews on Sunday 4 October.", "Sunday"),
        ("capitalised weekday abbreviation", "It renews on Sun 4 Oct.", "Sun"),
        ("month", "It renews in October.", "October"),
        ("month abbreviation", "It renews in Oct.", "Oct"),
        ("May in context", "The notice was sent in May.", "May"),
        ("ordinal day", "It renews on the 4th of October.", "the 4th"),
        ("ISO date", "It renews on 2026-10-04.", "2026-10-04"),
        ("dd.mm.yyyy date", "It renews on 04.10.2026.", "04.10.2026"),
        ("d/m/yyyy date", "It renews on 4/10/2026.", "4/10/2026"),
        ("countdown", "The renewal is 3 days away.", "3 days away"),
        ("counted duration", "The policy runs 12 months.", "12 months"),
        ("few days", "Expect a reply in a few days' time.", "a few days"),
    ],
)
def test_each_family_is_a_time_word(family: str, text: str, word: str) -> None:
    assert word in time_words(text), family


@pytest.mark.parametrize(
    "text",
    [
        "May I help with the reply?",
        "This is the 3rd time the price has dropped.",
        "Fridays are quiet at the garage.",
        "The sun was out and I sat down.",
        "Reply as soon as you can.",
        "The hearing entered the horizon.",
    ],
)
def test_these_are_not_time_words(text: str) -> None:
    assert time_words(text) == []
    assert relative_day_words(text) == []
