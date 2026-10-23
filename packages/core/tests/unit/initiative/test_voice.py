"""R9-256: the initiative's user-facing voice guard (``address_user``).

The A5-R-1 texts spoke about the user to the user. The rewrite is applied only where it
cannot break the grammar (an object or a possessive); anything else is refused.
"""

from __future__ import annotations

import pytest
from persona.initiative import address_user


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The A5-R-1 offers, as the model wrote them (T02, T03, T05).
        (
            "provide a shortlist of alternative car insurance offers for the user to review "
            "before 4 October",
            "provide a shortlist of alternative car insurance offers for you to review "
            "before 4 October",
        ),
        (
            "draft the response letter and show it to the user for approval",
            "draft the response letter and show it to you for approval",
        ),
        (
            "present a concise comparison of at least three alternative car insurance options "
            "for the user to review before 4 October 2026",
            "present a concise comparison of at least three alternative car insurance options "
            "for you to review before 4 October 2026",
        ),
        ("remind the user before the quote expires", "remind you before the quote expires"),
        ("tell the user whether it arrived", "tell you whether it arrived"),
        ("ask the user", "ask you"),
        ("show it to the user.", "show it to you."),
        ("send it to the user, then wait", "send it to you, then wait"),
        ("Show The User the draft", "Show you the draft"),
        ("The user's insurance renews on Sunday.", "Your insurance renews on Sunday."),
        ("compare the user’s offers", "compare your offers"),
        ("show the user's draft to the user", "show your draft to you"),
        # Already in the second person, or not about the user at all: unchanged.
        (
            "send you a reminder to place the order before the quote expires",
            "send you a reminder to place the order before the quote expires",
        ),
        (
            "draft the response letter for you to review",
            "draft the response letter for you to review",
        ),
        ("check the username field", "check the username field"),
        ("a user group meets on Friday", "a user group meets on Friday"),
    ],
)
def test_an_object_or_possessive_is_rewritten_to_you(text: str, expected: str) -> None:
    assert address_user(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # T01's reason, as the model wrote it: "the user" is the subject, so no safe rewrite.
        "The dinner party is within the next few days and the user expressed anxiety today.",
        "The user has not yet compared offers.",
        "Remind this user about the renewal.",
        "Explain that the user asked for this.",
        "The renewal is the user's.",  # a possessive with nothing after it reads wrong
        "a note on user's preferences",
        # Review MEDIUM-A: "the user" as the first half of a compound noun names a THING;
        # "send you manual" is garbled, so these void instead of rewriting.
        "send the user manual",
        "give the user ID to support",
        "show the user account settings",
        "tell the user group about it",
        "show the user interface changes",
        "send the user-agreement for signing",
        "remind the user Tuesday",  # a bare weekday is not a clause word either
    ],
)
def test_any_other_third_person_reference_is_refused(text: str) -> None:
    assert address_user(text) is None


@pytest.mark.parametrize(
    "text",
    [
        # Third review LOW 4: a follower that does not START a clause voids the text.
        "send the user to-do list",  # "to" followed by a hyphen: a compound
        "show the user in charge the draft",  # "in" is never a follower
        "update the user and group settings",  # "and" is never a follower
        "send it to the user and wait",  # precision first: voided, not risked
        "send the user review summary",  # a bare verb opens a clause only after let/help/make
    ],
)
def test_a_follower_that_does_not_start_a_clause_voids(text: str) -> None:
    assert address_user(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("let the user know by Friday", "let you know by Friday"),
        ("help the user decide whether to renew", "help you decide whether to renew"),
        ("make the user review it", "make you review it"),
        ("draft it for the user to review", "draft it for you to review"),
        ("send the user the draft", "send you the draft"),
    ],
)
def test_a_follower_that_starts_a_clause_is_rewritten(text: str, expected: str) -> None:
    assert address_user(text) == expected
