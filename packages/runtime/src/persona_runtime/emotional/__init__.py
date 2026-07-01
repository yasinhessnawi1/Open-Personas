"""Persona emotional expression (Spec N5) — text emotion adoption + feeling-tag→emoji.

Two halves: the versioned feeling-tag :mod:`.vocabulary` (the tag→emoji substitution
table) and the streaming-safe :mod:`.converter` (a pure, stateful filter that replaces
tags with emojis for chat and strips them for voice — guaranteeing no raw ``{{#…}}``
ever reaches the user on any path, criterion 3). The emotion-*adoption* prompt block
lives in :mod:`persona_runtime.prompt` (rendered below the character lock, N5-D-5).
"""

from __future__ import annotations

from persona_runtime.emotional.converter import (
    ConvertMode,
    FeelingTagConverter,
    convert_text,
)
from persona_runtime.emotional.vocabulary import (
    FEELING_TAG_VERSION,
    FEELING_TAGS,
    lookup_feeling,
)

__all__ = [
    "FEELING_TAGS",
    "FEELING_TAG_VERSION",
    "ConvertMode",
    "FeelingTagConverter",
    "convert_text",
    "lookup_feeling",
]
