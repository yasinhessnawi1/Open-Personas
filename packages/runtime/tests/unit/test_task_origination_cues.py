"""Unit tests for the multilingual standing-intent cue net (Spec A4, T4).

The net is the recall layer (NO/AR/EN): standing phrasings must fire (a miss is a silent
failure); now-work phrasings should stay on the cheap path. Precision is the model's job, so
a few false-admits are acceptable — these tests pin recall, not precision.
"""

from __future__ import annotations

import pytest
from persona_runtime.task_origination import detect_standing_cue

_STANDING_PHRASINGS = [
    # English
    "every morning, check the Oslo→Bergen fares",
    "each week send me a summary",
    "daily summary please",
    "keep an eye on the rental listing",
    "monitor the portal for updates",
    "remind me to water the plants",
    "from now on, draft my replies",
    "over the next week, track fares for me",
    "watch for a price drop",
    # Norwegian
    "hver morgen, sjekk prisene",
    "daglig oppsummering takk",
    "følg med på portalen",
    "hold øye med leieannonsen",
    "minn meg på møtet",
    "fra nå av, skriv utkast til svarene mine",
    "i løpet av uken, følg prisene",
    # Arabic
    "كل صباح راقب الأسعار",
    "يوميا أرسل لي ملخصا",
    "تابع هذا الموضوع",
    "ذكرني بالاجتماع",
    "من الآن أرسل لي تحديثات",
]

_NOW_WORK_PHRASINGS = [
    "summarise this article for me",
    "what's the capital of France?",
    "write a poem about the sea",
    "fix the bug on line 10",
    "translate this paragraph to Norwegian",
    "oppsummer denne artikkelen",  # NO: "summarise this article" — no standing cue
    "ما عاصمة فرنسا؟",  # AR: "what's the capital of France?" — no standing cue
]


@pytest.mark.parametrize("message", _STANDING_PHRASINGS)
def test_standing_phrasings_fire_a_cue(message: str) -> None:
    signal = detect_standing_cue(message)
    assert signal is not None, f"missed standing cue (silent failure) in: {message!r}"
    assert signal.marker  # the matched token is reported


@pytest.mark.parametrize("message", _NOW_WORK_PHRASINGS)
def test_now_work_phrasings_stay_on_cheap_path(message: str) -> None:
    assert detect_standing_cue(message) is None


def test_cue_is_case_insensitive() -> None:
    assert detect_standing_cue("EVERY MORNING check the news") is not None


def test_cue_reports_category() -> None:
    signal = detect_standing_cue("remind me to call the landlord")
    assert signal is not None
    assert signal.category == "remind"


def test_everything_does_not_falsely_fire_recurrence() -> None:
    # "everything" must not trip the "every <timeword>" recurrence pattern.
    assert detect_standing_cue("tell me everything about Rome") is None
