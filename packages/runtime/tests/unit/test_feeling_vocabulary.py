"""N5-A1 — the feeling-tag vocabulary artifact (N5-D-3, Spec-10 discipline).

The vocabulary is a versioned, frozen, strict-1:1 tag→emoji mapping. Cross-model
byte-identity (criterion 2) depends on 1:1 and immutability; the warm-biased,
no-anger/no-disgust policy (N5-D-3) is asserted here so a later widening is a
deliberate, tested, version-bumped change — not a silent drift.
"""

from __future__ import annotations

import re

import pytest
from persona_runtime.emotional import (
    FEELING_TAG_VERSION,
    FEELING_TAGS,
    lookup_feeling,
)

_NAME_RE = re.compile(r"^[a-z][a-z_]*[a-z]$")


def test_version_is_v1() -> None:
    assert FEELING_TAG_VERSION == "v1"


def test_has_twenty_eight_tags() -> None:
    assert len(FEELING_TAGS) == 28


def test_every_tag_maps_to_one_nonempty_emoji() -> None:
    # Strict 1:1 (criterion 2): exactly one emoji string per tag, never empty.
    for name, emoji in FEELING_TAGS.items():
        assert isinstance(emoji, str), name
        assert emoji, name


def test_all_emojis_distinct() -> None:
    # Distinct emojis keep the channel legible (a value collision would be a v1.1
    # review point, not a silent dupe).
    emojis = list(FEELING_TAGS.values())
    assert len(set(emojis)) == len(emojis)


def test_tag_names_are_lowercase_snake_and_self_vs_other_safe() -> None:
    for name in FEELING_TAGS:
        assert _NAME_RE.match(name), name


def test_mapping_is_immutable() -> None:
    # MappingProxyType — a frozen artifact can't be mutated at runtime.
    with pytest.raises(TypeError):
        FEELING_TAGS["happy"] = "🙃"  # type: ignore[index]


def test_lookup_known_and_unknown() -> None:
    assert lookup_feeling("happy") == "😊"
    assert lookup_feeling("proud_of_you") == "😌"
    assert lookup_feeling("nonsense") is None
    assert lookup_feeling("") is None


def test_policy_no_anger_no_disgust_no_directed_negative() -> None:
    # N5-D-3: warm-biased channel; no name that reads as directed-negatively-at-user.
    for banned in (
        "angry",
        "anger",
        "furious",
        "indignant",
        "disgust",
        "disgusted",
        "disappointed",
        "proud",
    ):
        assert banned not in FEELING_TAGS, banned
    # The disambiguated, other-directed forms are present.
    assert "proud_of_you" in FEELING_TAGS
    assert "sympathetic" in FEELING_TAGS
