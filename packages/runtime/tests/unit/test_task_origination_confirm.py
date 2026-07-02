"""Unit tests for the precision-biased confirmation interpreter (Spec A4, T6 loop wiring)."""

from __future__ import annotations

import pytest
from persona_runtime.task_origination import is_affirmative_confirmation


@pytest.mark.parametrize(
    "reply",
    [
        "yes",
        "Yes",
        "yes please",
        "go ahead",
        "do it",
        "confirm",
        "confirmed",
        "sounds good",
        "set it up",
        "ja",
        "gjør det",
        "sett i gang",
        "نعم",
        "yes!",
        "  go ahead. ",
    ],
)
def test_clean_affirmatives_confirm(reply: str) -> None:
    assert is_affirmative_confirmation(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "yes but make it 8am",  # an adjustment, not a clean confirm — must NOT create
        "actually no",
        "no",
        "cancel",
        "change the budget to 200kr",
        "what does that cost?",
        "hmm not sure",
        "yes if it's cheap",  # conditional — not a clean yes
        "",
    ],
)
def test_non_clean_replies_do_not_confirm(reply: str) -> None:
    # The load-bearing safety bias: only a whole-message affirmative confirms.
    assert is_affirmative_confirmation(reply) is False
