"""B9 (ruling after the third MID set): "tomorrow" directly before the full date it names is
removed and the date kept; every other relative use is refused as before, "today" included
(review LOW-1: a notice about today is held to the next morning and lapses unseen).

The pure rule here (:func:`normalise_relative_days`); the pipeline's use of it (before the
judge, fingerprinted, delivered, audited, in the owner's zone) in ``test_pipeline.py``.
"""

from __future__ import annotations

from datetime import date

import pytest
from persona_runtime.initiative.specifics import normalise_relative_days, relative_day_words

_TODAY = date(2027, 3, 11)  # a Thursday


@pytest.mark.parametrize(
    ("text", "expected", "removed"),
    [
        (
            "Your choir concert is tomorrow, Friday 12 March 2027, at the town hall.",
            "Your choir concert is on Friday 12 March 2027, at the town hall.",
            ["tomorrow"],
        ),
        (
            "The rowing club's dues are tomorrow 12 March 2027.",
            "The rowing club's dues are on 12 March 2027.",
            ["tomorrow"],
        ),
        (
            "Rehearsal starts tomorrow, Friday 12 March 2027.",
            "Rehearsal starts Friday 12 March 2027.",
            ["tomorrow"],
        ),
        (
            "Tomorrow, Friday 12 March 2027, the hall opens early.",
            "Friday 12 March 2027, the hall opens early.",
            ["Tomorrow"],
        ),
        (
            "It's tomorrow, March 12, 2027.",
            "It's on March 12, 2027.",
            ["tomorrow"],
        ),
    ],
)
def test_a_relative_word_before_its_own_date_is_removed(
    text: str, expected: str, removed: list[str]
) -> None:
    normalised, words = normalise_relative_days(text, today=_TODAY)
    assert normalised == expected
    assert words == removed
    assert relative_day_words(normalised, dated=True) == []


@pytest.mark.parametrize(
    "text",
    [
        "Your choir concert is tomorrow, Saturday 13 March 2027.",  # a different day
        "Your choir concert is tomorrow, 13 March 2027.",  # a different day, no weekday
        "Your choir concert is tomorrow, Saturday 12 March 2027.",  # the weekday is wrong
        "Your choir concert is today, Friday 12 March 2027.",  # today is not the 12th
        "The vote is today, Thursday 11 March 2027.",  # today's own date: still refused
        "Today, Thursday 11 March 2027, the vote opens.",  # the same, first in the sentence
        "Your choir concert is tomorrow.",  # no adjacent date
        "Your choir concert is tomorrow, 12 March.",  # not a full date (no year)
        "Your choir concert is next week, Friday 12 March 2027.",  # another relative word
        "Tomorrow the hall opens; the concert is on Friday 12 March 2027.",  # not adjacent
    ],
)
def test_anything_else_is_left_for_the_refusal(text: str) -> None:
    normalised, words = normalise_relative_days(text, today=_TODAY)
    assert normalised == text
    assert words == []
    assert relative_day_words(normalised, dated=True) != []
