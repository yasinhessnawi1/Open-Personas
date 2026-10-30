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
        ("vague hedge", "It renews on 4 October 2026, so act soon.", "soon"),
        ("vague time-sufficiency", "You still have a few days to act.", "still have a few days"),
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
        "You might want to compare offers.",
        "It might be worth a look.",
        "You may want to book a slot.",
        "You could ask the garage.",
        "You checked the price on Power.",
        "You may be able to book online.",
    ],
)
def test_these_are_not_time_words(text: str) -> None:
    assert time_words(text) == []
    assert relative_day_words(text) == []


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("With your MOT coming up, you may want to book.", "coming up"),
        ("The renewal keeps coming up in your notes.", "coming up"),
        ("You have time to compare offers.", "have time"),
        ("There's still time to compare offers.", "There's still time"),
        ("There is time before the renewal.", "There is time"),
        ("You have no time left.", "have no time"),
        ("Nothing is booked and the time left is short.", "time left"),
        ("You are running out of time.", "running out of time"),
        ("It is not time yet.", "time yet"),
        ("You have enough time.", "have enough time"),
        ("Leave enough time for the inspection.", "enough time"),
        ("The renewal is nearly due.", "nearly"),
        ("The renewal is close.", "close"),
        ("Not long to go before the renewal.", "long to go"),
        ("The renewal is around the corner.", "around the corner"),
        ("Nothing to do 'til the renewal.", "'til"),
    ],
)
def test_the_broad_time_words_are_re_judged(text: str, word: str) -> None:
    """Third review LOW-1: the refusal stays narrow, but anything that may read stale on
    another day is a time word, so the flush re-asks the judge about it."""
    assert word in time_words(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Compare offers when you have time.",
        "If you have time, I can compare offers for you.",
        "You have time-sensitive paperwork for the renewal.",
        "You have time off planned around the renewal.",
        "You have time pressure at work this autumn.",
        "You have time zones to juggle for the renewal call.",
        "The renewal keeps coming up in your notes.",
        "There are questions coming up in your driving test.",
        "With your MOT coming up, you may want to book.",
    ],
)
def test_the_false_refusal_probes_are_still_not_refused(text: str) -> None:
    assert relative_day_words(text) == []


@pytest.mark.parametrize(
    ("text", "word"),
    [
        (
            "You noted the deadline might fall before Christmas but that you hadn't checked.",
            "might fall",
        ),
        ("The grant deadline might be before Christmas.", "might be before"),
        ("The form may be due in November.", "may be due"),
        ("It could be sometime in spring.", "could be sometime"),
        ("You have not checked the deadline yet.", "have not checked"),
        ("You hadn't checked when it closes.", "hadn't checked"),
        ("The deadline is probably before the holidays.", "probably before"),
        ("The dues are maybe in spring.", "maybe"),
        ("It is possibly due in spring.", "possibly"),
        ("It falls due perhaps in spring.", "perhaps"),
        ("You are not sure when the dues fall due.", "not sure when"),
        ("You are not sure if it is spring.", "not sure if"),
        ("I think the dues fall in spring.", "I think the"),
        ("I think it is in spring.", "I think it"),
        ("The date is unconfirmed.", "unconfirmed"),
        ("You have not confirmed the date.", "have not confirmed"),
        ("The club hasn't verified the date.", "hasn't verified"),
        ("The date is not yet checked.", "not yet checked"),
    ],
)
def test_a_speculated_date_is_re_judged(text: str, word: str) -> None:
    """B11 (ruling after the second MID set): a message resting on a date the material
    only guesses at is never kept at the flush; the v5 judge is asked again. It is not
    refused mechanically: the judge decides."""
    assert word in time_words(text), text
    assert relative_day_words(text, dated=True) == []
