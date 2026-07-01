"""The feeling-tag vocabulary — a versioned, frozen, strict-1:1 tag→emoji map (N5-D-3).

The persona emits **semantic feeling-tags** (``{{#happy}}``) rather than raw emojis;
:mod:`.converter` replaces each with its one canonical emoji, **byte-identical across
models** (criterion 2). This module is the single source of truth for *which* tags exist
and *what* each maps to — imported by both the converter (to substitute) and the prompt
(to tell the model the vocabulary).

**Spec-10 discipline (mirrors ``CHARACTER_LOCK_VERSION`` / ``EXTRACTION_PROMPT_VERSION``):**
``FEELING_TAG_VERSION`` is bumped on **any** tag add/remove/rename or emoji change, and the
N5 bidirectional restraint eval (N5-D-6) re-runs per version — so a vocabulary change is a
traceable, re-measured event, never a silent drift.

**Policy (N5-D-3):** warm-biased companion register — rich on joy / warmth / pride-in-you /
encouragement / curiosity / care; negatives limited to *shared, mild* (concern, sympathy,
situation-sadness). **No anger, no disgust, and no name that can read as directed-negatively-
at-the-user** (hence ``proud_of_you`` not ``proud``; ``disappointed`` is intentionally absent —
``sympathetic`` carries the situation-letdown warmth). Strict 1:1: exactly one emoji per tag,
richness from taxonomy breadth, never per-tag rotation (rotation would break byte-identity).

**Known v1 limitation (documented + owned, N5-D-2):** a well-formed *unknown* ``{{#word}}`` is
stripped (criterion 3 is supreme — unknown tags never show raw). This collides only with block
templating syntax — a persona actively *teaching* Handlebars/Mustache ``{{#each}}`` block forms
loses that token. ``{{name}}`` interpolation is unaffected (only ``{{#…}}`` block forms match).
The fix is deferred to v1.1 as an additive escape / code-fence exemption (a sentinel change would
be a cross-cutting rework of vocabulary + converter + tests + the V12 voice reuse); the versioned
``FEELING_TAG_VERSION`` lets that ship cleanly later without touching v1's guarantee.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

__all__ = [
    "FEELING_TAGS",
    "FEELING_TAG_VERSION",
    "lookup_feeling",
    "render_prompt_palette",
]

#: Bump on any tag add/remove/rename or emoji change; the N5 restraint eval (N5-D-6)
#: re-runs per version so the change is traceable + re-measured (Spec-10 discipline).
FEELING_TAG_VERSION: Final = "v1"

# The frozen v1 vocabulary (28 tags, strict 1:1). Grouped by family for legibility;
# every emoji is distinct. Bounded by character at emission time (N5-D-5) — this map
# is only the substitution table.
_FEELING_TAGS: Final[dict[str, str]] = {
    # Joy / delight
    "happy": "😊",
    "joyful": "😄",
    "delighted": "😁",
    "amused": "😆",
    "playful": "😜",
    # Warmth / affection / trust
    "warm": "🤗",
    "grateful": "🙏",
    "fond": "☺️",
    "affectionate": "🥰",
    # Pride-in-you / approval (other-directed — never bare "proud")
    "proud_of_you": "😌",
    "pleased": "🙂",
    "impressed": "👏",
    # Interest / anticipation
    "curious": "🤔",
    "excited": "🤩",
    "eager": "✨",
    "intrigued": "🧐",
    "hopeful": "🤞",
    # Care / support / empathy
    "supportive": "💪",
    "encouraging": "🙌",
    "sympathetic": "🫂",
    "reassuring": "🤝",
    # Concern — shared and mild
    "concerned": "😟",
    "worried": "😰",
    # Sadness — shared and mild
    "sad": "😢",
    "wistful": "😔",
    # Surprise
    "surprised": "😮",
    "amazed": "😲",
    # Reflection
    "thoughtful": "💭",
}

#: The frozen, read-only vocabulary. ``MappingProxyType`` makes the artifact
#: immutable at runtime — a widening must go through a version bump, not a mutation.
FEELING_TAGS: Final = MappingProxyType(_FEELING_TAGS)


def lookup_feeling(name: str) -> str | None:
    """Return the canonical emoji for ``name``, or ``None`` if it isn't a known tag.

    ``None`` is the converter's signal to **strip** an unknown/malformed tag (never
    show it raw — criterion 3). Case-sensitive: tags are lowercase snake_case.
    """
    return FEELING_TAGS.get(name)


def render_prompt_palette() -> str:
    """The available feeling-tag names for the emotion-adoption prompt block (N5-D-5).

    Rendered from the frozen vocabulary (family/insertion order) so the prompt's
    palette and the converter's substitution table can never drift — a tag change
    is one edit here, versioned by ``FEELING_TAG_VERSION``. Names only (the model
    emits ``{{#name}}``); the emoji mapping is the converter's private business.
    """
    return ", ".join(FEELING_TAGS)
