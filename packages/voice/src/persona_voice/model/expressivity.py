"""Spec V12 — the persona-voice expressivity map (V12-D-3, V12-D-6).

Maps N5's derived emotional **stance** (feeling-tags) → Cartesia Sonic-3.5
``generation_config`` expressivity, so the persona *sounds* its feeling. A pure,
frozen, **versioned data asset** (``V12_EXPRESSIVITY_VERSION``, mirroring N5's
``FEELING_TAG_VERSION`` discipline): a value change is a traceable, re-measured event
(the shape-eval + the real-voice operator pass re-run per version — V12-D-7).

**Restraint by construction (V12-D-3, criterion 2 — over-emoted TTS is worse than
flat).** Only the **warm subset** of Cartesia's emotions is ever targeted — the
hostile set (angry / outraged / disgusted / …) is exactly what N5 excludes, so the
map *cannot* over-emote into hostility. Speed/volume stay within small deltas of 1.0.
A no-tag utterance (the default, and a stoic/reserved persona) resolves to ``None`` =
today's clean flat read.

**Layered robustness across the stable/Beta boundary (V12-D-6).** Cartesia's
``speed``/``volume`` are stable; ``emotion`` is Beta. :class:`VoiceExpressivity`
carries both: ``speed``/``volume`` are the **resilient base layer**,
``emotion`` the **additive richer layer**. :meth:`VoiceExpressivity.to_generation_config`
can drop ``emotion`` independently (``include_emotion=False``), so a Beta regression
degrades to subtle pace/volume expressivity — then (worst case) to flat — never
all-or-nothing.

**Adapter discipline (Spec 02).** This module is provider-agnostic: it emits a plain
``dict`` of generation-config values; only ``cartesia_backend`` imports the vendor SDK
and maps that dict onto the SDK's ``GenerationConfigParam``. The emotion strings are
validated against the installed SDK's emotion Literal in the T2 test (V12-D-6), not by
importing the SDK here.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "EXPRESSIVITY_MAP",
    "V12_EXPRESSIVITY_VERSION",
    "VoiceExpressivity",
    "VoiceExpressivityChannel",
    "resolve_expressivity",
]

#: Bump on any value/mapping change; the V12 shape-eval + the real-voice operator pass
#: (V12-D-7) re-run per version, so a change is traceable + re-ratified by the ear.
V12_EXPRESSIVITY_VERSION: Final = "v1"


class VoiceExpressivity(BaseModel):
    """A resolved per-utterance expressivity: the stance rendered as TTS controls.

    Frozen. ``speed``/``volume`` (the stable base layer) are constrained to Cartesia's
    valid ranges so a bad edit fails fast at construction (mirrors ``StreamingTTSConfig``
    fail-fast). ``emotion`` (the Beta additive layer) is a Cartesia emotion name or
    ``None``; it is validated against the SDK Literal in the T2 test (V12-D-6).
    """

    model_config = ConfigDict(frozen=True)

    #: The Beta additive layer — a Cartesia emotion name (warm subset), or ``None``.
    emotion: str | None = None
    #: The stable base layer — speech rate, Cartesia range [0.6, 1.5]; ``None`` ⇒ unset.
    speed: float | None = Field(default=None, ge=0.6, le=1.5)
    #: The stable base layer — volume, Cartesia range [0.5, 2.0]; ``None`` ⇒ unset.
    volume: float | None = Field(default=None, ge=0.5, le=2.0)

    def to_generation_config(self, *, include_emotion: bool = True) -> dict[str, float | str]:
        """The provider-agnostic ``generation_config`` dict (None fields omitted).

        ``include_emotion=False`` drops the Beta ``emotion`` layer while keeping the
        stable ``speed``/``volume`` base (V12-D-6 graceful degradation). An all-unset
        expressivity yields ``{}`` — the caller sends no ``generation_config`` (flat).
        """
        cfg: dict[str, float | str] = {}
        if self.speed is not None:
            cfg["speed"] = self.speed
        if self.volume is not None:
            cfg["volume"] = self.volume
        if include_emotion and self.emotion is not None:
            cfg["emotion"] = self.emotion
        return cfg


# --- The four restraint profiles (speed, volume) — small deltas (V12-D-3) ----------
# Emotion is per-tag (below); pacing/volume clusters into four energy/tenderness bands.
_BRIGHT: Final = (1.06, 1.04)  # high warm energy
_WARM: Final = (1.00, 1.00)  # gentle warmth, neutral pacing (emotion carries it)
_TENDER: Final = (0.97, 0.98)  # soft, caring, a touch slower
_SUBDUED: Final = (0.95, 0.96)  # gentle, wistful


def _expr(emotion: str, profile: tuple[float, float]) -> VoiceExpressivity:
    return VoiceExpressivity(emotion=emotion, speed=profile[0], volume=profile[1])


# The frozen v1 map — every N5 feeling-tag → a warm-subset expressivity (V12-D-3).
# Coverage of the full N5 vocabulary is enforced by the T2 drift-guard test.
_EXPRESSIVITY_MAP: Final[dict[str, VoiceExpressivity]] = {
    # Joy / delight
    "happy": _expr("happy", _WARM),
    "joyful": _expr("happy", _BRIGHT),
    "delighted": _expr("happy", _BRIGHT),
    "amused": _expr("happy", _BRIGHT),
    "playful": _expr("happy", _BRIGHT),
    # Warmth / affection / trust
    "warm": _expr("affectionate", _WARM),
    "grateful": _expr("grateful", _WARM),
    "fond": _expr("affectionate", _WARM),
    "affectionate": _expr("affectionate", _WARM),
    # Pride-in-you / approval
    "proud_of_you": _expr("proud", _WARM),
    "pleased": _expr("content", _WARM),
    "impressed": _expr("amazed", _BRIGHT),
    # Interest / anticipation
    "curious": _expr("curious", _WARM),
    "excited": _expr("excited", _BRIGHT),
    "eager": _expr("anticipation", _WARM),
    "intrigued": _expr("curious", _WARM),
    "hopeful": _expr("anticipation", _WARM),
    # Care / support / empathy
    "supportive": _expr("confident", _WARM),
    "encouraging": _expr("confident", _WARM),
    "sympathetic": _expr("sympathetic", _TENDER),
    "reassuring": _expr("calm", _TENDER),
    # Concern — shared and mild; CARING, never the persona sounding anxious (V12-D-3)
    "concerned": _expr("sympathetic", _TENDER),
    "worried": _expr("sympathetic", _TENDER),
    # Sadness — shared and mild
    "sad": _expr("sad", _SUBDUED),
    "wistful": _expr("wistful", _SUBDUED),
    # Surprise
    "surprised": _expr("surprised", _WARM),
    "amazed": _expr("amazed", _BRIGHT),
    # Reflection
    "thoughtful": _expr("contemplative", _TENDER),
}

#: The frozen, read-only map. ``MappingProxyType`` makes it immutable at runtime — a
#: change must go through a ``V12_EXPRESSIVITY_VERSION`` bump, not a mutation.
EXPRESSIVITY_MAP: Final[Mapping[str, VoiceExpressivity]] = MappingProxyType(_EXPRESSIVITY_MAP)


def resolve_expressivity(tags: Sequence[str]) -> VoiceExpressivity | None:
    """Resolve an utterance's captured feeling-tags → its expressivity, or ``None``.

    **First-recognized-tag-wins** (V12-D-2 lead-with-the-tag): the persona is instructed
    to lead with its stance, so the first captured tag is the utterance's stance — a
    second tag cannot produce an ambiguous config. An unmapped tag is skipped (robust to
    N5 widening its vocabulary ahead of V12); if no tag is recognised, ``None`` ⇒ today's
    clean flat read (the default for most utterances and for stoic personas).
    """
    for tag in tags:
        expr = EXPRESSIVITY_MAP.get(tag)
        if expr is not None:
            return expr
    return None


class VoiceExpressivityChannel:
    """Per-session hand-off of the resolved per-utterance expressivity (V12-D-4).

    The reply producer captures the persona's stance tag and **publishes** the
    resolved :class:`VoiceExpressivity` here; the TTS backend **takes** it at
    synthesis time to drive Cartesia's ``generation_config``. This is the additive
    threading seam that keeps the ``StreamingTTS.synthesize`` Protocol text-only (no
    Protocol change) and the controls out-of-band (never in the text stream, V12-D-1).

    **Not global state.** One instance per session, created at the composition root
    and injected into exactly one producer + one backend — so two concurrent calls
    (or personas) never share a slot. The backend :meth:`reset`\\ s it at the start of
    each utterance so a prior utterance's stance cannot bleed into the next
    (a sad line does not colour the following neutral one). Single-event-loop
    ordering makes this deterministic: the backend resets before it pulls any token,
    and the producer publishes only while its tokens are being pulled.

    All three methods are trivial attribute access (no I/O, no allocation) — the
    voice-loop rule (never starve the loop) is respected by construction.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: VoiceExpressivity | None = None

    def publish(self, expr: VoiceExpressivity | None) -> None:
        """Producer side: record this utterance's resolved expressivity (or ``None``)."""
        self._pending = expr

    def take(self) -> VoiceExpressivity | None:
        """Backend side: the pending expressivity, or ``None`` (⇒ flat read)."""
        return self._pending

    def reset(self) -> None:
        """Backend side: clear before an utterance so a prior stance cannot bleed."""
        self._pending = None
