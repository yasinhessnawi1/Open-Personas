"""Owner ruling 2026-11-05: what the user reads carries no em or en dash.

The pure rule (:func:`normalise_dashes`); the pipeline's use of it (before the judge,
stored, audited, ahead of the "tomorrow" normalisation) is in ``test_pipeline.py``. The
dashes are written with ``chr`` because the source tree carries none.
"""

from __future__ import annotations

from datetime import date

import pytest
from persona_runtime.initiative.specifics import normalise_dashes, unsupported_specifics

EM = chr(0x2014)
EN = chr(0x2013)


@pytest.mark.parametrize(
    ("text", "expected", "kinds"),
    [
        # a spaced dash is a comma, em or en
        (f"Rehearsal is on 12 March 2027 {EM} bring the score.",
         "Rehearsal is on 12 March 2027, bring the score.", ["comma"]),
        (f"Rehearsal is on 12 March 2027 {EN} bring the score.",
         "Rehearsal is on 12 March 2027, bring the score.", ["comma"]),
        # an em dash in prose, unspaced, is a comma too
        (f"The hall{EM}newly painted{EM}opens early.",
         "The hall, newly painted, opens early.", ["comma", "comma"]),
        # no doubled comma, and nothing left hanging at either end
        (f"It is open, {EM} go ahead.", "It is open, go ahead.", ["comma"]),
        (f"It is open {EM}", "It is open", ["comma"]),
        (f"{EM} It is open.", "It is open.", ["comma"]),
        # a range of times or numbers reads "to"
        (f"It runs 19:30{EN}21:00 on Friday.", "It runs 19:30 to 21:00 on Friday.", ["to"]),
        (f"It runs 19:30 {EN} 21:00 on Friday.", "It runs 19:30 to 21:00 on Friday.", ["to"]),
        (f"It runs 19.30{EM}21.00 on Friday.", "It runs 19.30 to 21.00 on Friday.", ["to"]),
        (f"Pages 12{EN}18 are new.", "Pages 12 to 18 are new.", ["to"]),
        (f"Seats cost 1,200{EN}1,500 kr.", "Seats cost 1,200 to 1,500 kr.", ["to"]),
        # an en dash between names is a hyphen
        (f"The Tromso{EN}Narvik coach leaves.", "The Tromso-Narvik coach leaves.", ["hyphen"]),
        (f"The Q1{EN}Q2 plan is ready.", "The Q1-Q2 plan is ready.", ["hyphen"]),
    ],
)  # fmt: skip
def test_each_dash_is_replaced(text: str, expected: str, kinds: list[str]) -> None:
    assert normalise_dashes(text) == (expected, kinds)


@pytest.mark.parametrize(
    "text",
    [
        "A well-known hall, booked for 19:30 to 21:00.",
        "It costs 7,950 kr - or so the note says.",  # a plain hyphen is not ours to touch
        "",
    ],
)
def test_a_text_without_em_or_en_dashes_is_untouched(text: str) -> None:
    assert normalise_dashes(text) == (text, [])


@pytest.mark.parametrize(
    ("message", "excerpt"),
    [
        (f"It runs 19:30{EN}21:00 on Friday 12 March 2027.",
         "The rehearsal on Friday 12 March 2027 runs 19:30 to 21:00."),
        (f"It runs 19:30 to 21:00 {EM} on Friday 12 March 2027.",
         f"The rehearsal on Friday 12 March 2027 runs 19:30{EN}21:00."),
        (f"The fee is 7,950 kr {EM} due on 1 April 2027.",
         "The fee of 7 950 kr is due on 1 April 2027."),
        (f"The dues are due on 12 March 2027 {EM} 18:00 at the latest.",
         "The dues are due by 18:00 on 12 March 2027."),
    ],
)  # fmt: skip
def test_the_specifics_check_still_matches_the_numbers(message: str, excerpt: str) -> None:
    normalised, _ = normalise_dashes(message)
    assert normalised is not None
    assert unsupported_specifics(normalised, [excerpt], today=date(2027, 3, 1)) == []


def test_a_number_the_grounds_do_not_state_is_still_caught() -> None:
    normalised, _ = normalise_dashes(f"It runs 19:30{EN}22:00 on 12 March 2027.")
    assert normalised is not None
    found = unsupported_specifics(
        normalised, ["It runs 19:30 to 21:00 on 12 March 2027."], today=date(2027, 3, 1)
    )
    assert found


@pytest.mark.parametrize(
    ("text", "expected", "kinds"),
    [
        # a minus sign stays a minus sign
        (f"Your balance is {EN}500 kr.", "Your balance is -500 kr.", ["minus"]),
        (f"{EN}500 kr is owed.", "-500 kr is owed.", ["minus"]),
        (f"The balance ({EN}500 kr) is due.", "The balance (-500 kr) is due.", ["minus"]),
        # an em dash is never a minus (re-review LOW-2)
        (f"It is the trip {EM}2 days away.", "It is the trip, 2 days away.", ["comma"]),
        (f"Quotes {EM}500, 650 and 700.", "Quotes, 500, 650 and 700.", ["comma"]),
        (f"The balance ({EM}500 kr) is due.", "The balance (500 kr) is due.", ["comma"]),
        # a bare weekday then the date it falls on is an aside, not a range (re-review LOW-1)
        (f"It is on Friday {EM} 2 October 2026 {EM} at the county court.",
         "It is on Friday, 2 October 2026, at the county court.", ["comma", "comma"]),
        (f"It runs Monday {EN} 2 October 2026.", "It runs Monday to 2 October 2026.", ["to"]),
        # a spaced dash between two numbers is a range
        (f"Seats cost 500 {EN} 700 kr.", "Seats cost 500 to 700 kr.", ["to"]),
        (f"It runs 1 {EN} 3 October.", "It runs 1 to 3 October.", ["to"]),
        # unless the left number is the year that ends a date
        (f"It is on 1 October 2026 {EM} 15 people are coming.",
         "It is on 1 October 2026, 15 people are coming.", ["comma"]),
        # a spaced dash between dates or weekdays is a range
        (f"It runs 29 September {EN} 2 October.", "It runs 29 September to 2 October.", ["to"]),
        (f"It runs 1 October 2026 {EN} 2 October 2026.",
         "It runs 1 October 2026 to 2 October 2026.", ["to"]),
        (f"It runs Monday {EN} Friday.", "It runs Monday to Friday.", ["to"]),
        (f"It runs Monday 28 September {EM} Friday 2 October.",
         "It runs Monday 28 September to Friday 2 October.", ["to"]),
        (f"It runs March {EN} May.", "It runs March to May.", ["to"]),
        # an ISO date keeps its hyphens
        (f"It is due on 2026{EN}10{EN}04.", "It is due on 2026-10-04.", ["hyphen"]),
        # no artefacts around other punctuation
        (f"It is open. {EM} Go ahead.", "It is open. Go ahead.", ["comma"]),
        (f"Is it open? {EM} Go ahead.", "Is it open? Go ahead.", ["comma"]),
        (f"It is open {EM}, go ahead.", "It is open, go ahead.", ["comma"]),
        (f"It is open {EM}.", "It is open.", ["comma"]),
    ],
)  # fmt: skip
def test_a_number_or_a_range_keeps_its_meaning(text: str, expected: str, kinds: list[str]) -> None:
    assert normalise_dashes(text) == (expected, kinds)


@pytest.mark.parametrize(
    "text",
    [
        f"Seats cost 500 kr {EN} 700 kr.",  # a range or a list: the unit hides it
        f"Seats cost kr 500 {EN} kr 700.",
        f"Seats cost 500 {EN}700 kr.",  # a range or a minus
        f"Seats cost 500{EN} 700 kr.",
        f"The doors open at 19:30 {EM} 40 people are coming.",  # a time and a number
        f"{EM} 500 kr is owed.",  # a bullet or a minus
    ],
)
def test_a_dash_that_could_change_a_number_voids_the_text(text: str) -> None:
    assert normalise_dashes(text) == (None, ["ambiguous_number"])


@pytest.mark.parametrize("text", [EM, f" {EN} ", f"{EM} {EM}"])
def test_a_text_that_empties_voids(text: str) -> None:
    assert normalise_dashes(text) == (None, ["emptied"])
