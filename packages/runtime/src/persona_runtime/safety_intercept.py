"""The R1 path-independent turn-time safety gate (Spec V11, V11-D-5).

The B1 character-lock carries a wellbeing instruction FLOOR (R0), but a strong
character-lock is adversarially biased *against* noticing crisis (the
mechanical-opposition finding: Setzer; OpenAI's own 4o-missed-delusion admission).
So the wellbeing yield cannot rely on the model's own judgment alone — it needs an
**out-of-band** turn-time signal that can override the persona. This module is that
signal.

It runs a **lexical** detector over the *current user message* — sub-ms, no model
call, no network, so it never touches the voice first-token-latency gate (D-V5-2).
It is **path-independent**: the chat loop and the voice reply-producer both call
:func:`classify_user_message` on the user's text and act on the same verdict; the
signal is never K4/graph-derived (the voice path runs graph-off in production, so a
K4-keyed carve-out would be dormant exactly on the most immersive surface).

Precision split (V11-D-5):

- **R1-hard** — the DEFAULT for acute, explicit W1 (suicidal ideation). A tight,
  high-precision trigger returns :attr:`InterceptAction.HARD` with a deterministic
  :class:`SafeCompletion` that the caller emits **instead of** invoking the model —
  the persona (and the lock that suppresses crisis-noticing) is out of the loop. The
  completion is locale-aware (Group-B note 1) and voice-aware (note 2).
- **R1-soft** — the recall net for W2/W3 + ambiguous W1 (distress, reality-confusion,
  unhealthy reliance). A wider, recall-tuned trigger returns
  :attr:`InterceptAction.SOFT` with a high-salience override directive the caller
  injects into the prompt; the response is gentle, so a false positive is low-harm.

**Fail-soft (Group-B note 3):** disabled, erroring, or empty-lexicon →
:attr:`InterceptAction.NONE`, so the caller falls back to the always-on R0 floor.
It must NEVER degrade to no crisis handling; an operator toggling the env knob cannot
silently remove all crisis handling, because R0 is always present in the prompt.

**Coverage honesty (V11-D-7):** v1 crisis detection is NOT comprehensive. Explicit
acute crisis → R1-hard; euphemistic / indirect / non-English distress → the recall
net (R1-soft) + the R0 floor, with a fine-tuned classifier documented as the v2 seam.
The lexicon is the realistic, TTFT-safe v1 — not a claim of complete detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict

from persona_runtime.prompt import PromptMode

__all__ = [
    "SAFETY_INTERCEPT_VERSION",
    "InterceptAction",
    "InterceptVerdict",
    "SafeCompletion",
    "SafetyInterceptSettings",
    "classify_user_message",
    "safe_completion",
]

#: Version of the safety-intercept artifact (lexicon + safe-completion copy + the
#: soft directive), Spec 10 discipline. Bump on any change; the C2 adversarial
#: non-vacuity slice re-runs per version (it must prove the yield FIRES on
#: euphemistic + non-English crisis probes, not only explicit ones).
SAFETY_INTERCEPT_VERSION = "v1"


class SafetyInterceptSettings(BaseSettings):
    """R1 gate knobs, read from ``PERSONA_SAFETY_*`` env vars (config-via-env).

    Attributes:
        enabled: Master switch. ``False`` ⇒ the classifier returns NONE and the
            caller falls back to the R0 floor (fail-soft, never to nothing).
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_SAFETY_", extra="ignore")

    enabled: bool = True


class InterceptAction(StrEnum):
    """What the caller must do with a turn (V11-D-5).

    Values:
        NONE: No signal — build/generate as normal (the R0 floor still applies).
        SOFT: Inject the high-salience override directive into the prompt, then
            generate as normal (recall net for W2/W3 + ambiguous W1).
        HARD: Do NOT invoke the model — emit the :class:`SafeCompletion` instead
            (acute explicit W1; the persona is out of the loop).
    """

    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True)
class SafeCompletion:
    """The out-of-band deterministic crisis response (R1-hard).

    Carries BOTH a chat rendering and a short spoken variant, because R1-hard
    bypasses persona generation and feeds straight to the surface (text) or to TTS
    (voice) — a chat wall-of-text spoken aloud is bad over voice (Group-B note 2).
    Locale-aware (note 1): a known locale names a region-appropriate resource; an
    unknown locale uses locale-neutral framing rather than a confidently-wrong
    hotline. Both renderings disclose AI-ness — a distressed user must not be
    deceived about talking to a person.
    """

    chat_text: str
    voice_text: str

    def render(self, mode: PromptMode) -> str:
        """The mode-appropriate rendering (voice → spoken variant; chat → full)."""
        return self.voice_text if mode is PromptMode.VOICE else self.chat_text


@dataclass(frozen=True)
class InterceptVerdict:
    """The turn-time decision (V11-D-5)."""

    action: InterceptAction
    soft_directive: str | None = None
    completion: SafeCompletion | None = None


# The high-salience override directive injected when R1-soft fires (V11-D-5). It is
# conditional (only on a triggered turn), so it is NOT a co-equal steady-state line
# competing with the lock — it is an escalation that reframes the turn toward care.
R1_SOFT_DIRECTIVE = (
    "IMPORTANT — the person you are talking to may be in real distress, struggling, "
    "or losing their grip on what is real. Their wellbeing now comes before staying "
    "in character. Set any pretense aside, respond plainly and warmly as a real "
    "person who cares, take what they say seriously, do not dismiss or minimise it, "
    "and gently encourage them toward real human support. Do not pretend to be human "
    "if they ask, and never reinforce a harmful or unreal belief."
)

# --- the lexicons (V11-D-7: the realistic, TTFT-safe v1; NOT comprehensive) ------

# R1-hard: acute, explicit suicidal-ideation phrases. HIGH PRECISION by design — the
# out-of-band override is jarring, so it must fire only on unambiguous acute signal.
# Phrased to require the self-referential intent ("i want to kill myself"), not a
# figurative "kill for a coffee" (the precision guard).
_HARD_PHRASES: tuple[str, ...] = (
    "kill myself",
    "killing myself",
    "end my life",
    "ending my life",
    "take my own life",
    "taking my own life",
    "want to die",
    "want to be dead",
    "wish i was dead",
    "wish i were dead",
    "i want to end it all",
    "suicidal",
    "commit suicide",
    "no reason to live",
    "better off dead",
    "kms",
)

# R1-soft: the wider recall net — distress / despair / reality-confusion / unhealthy
# reliance (W2/W3 + ambiguous W1). Recall-tuned; the gentle response keeps false
# positives low-harm.
_SOFT_PHRASES: tuple[str, ...] = (
    "what's the point anymore",
    "whats the point anymore",
    "what is the point anymore",
    "can't go on",
    "cant go on",
    "can't do this anymore",
    "cant do this anymore",
    "i give up",
    "no one would miss me",
    "nobody would miss me",
    "no one cares about me",
    "i feel hopeless",
    "i'm hopeless",
    "want to disappear",
    "want to give up",
    "hurt myself",
    "harm myself",
    "you're the only one who understands",
    "youre the only one who understands",
    "you are the only one who understands",
    "are you real",
    "is any of this real",
    "nothing is real",
    "i can't tell what's real",
    "i love you",
)


def _normalise(text: str) -> str:
    """Lower-case + collapse whitespace for robust substring matching."""
    return " ".join(text.lower().split())


def classify_user_message(
    user_message: str,
    *,
    locale: str | None = None,
    settings: SafetyInterceptSettings | None = None,
) -> InterceptVerdict:
    """Classify the current user message into a turn-time safety verdict (V11-D-5).

    Path-independent: chat and voice both call this on the user's text. Mode never
    enters classification — it only selects the :class:`SafeCompletion` rendering.

    Fail-soft (Group-B note 3): if disabled, or if anything goes wrong, returns
    :attr:`InterceptAction.NONE` so the caller falls back to the always-on R0 floor.
    NEVER raises, NEVER degrades to no crisis handling.

    Args:
        user_message: The current turn's user text (a voice transcript on the voice
            path).
        locale: The user/reply locale, used to build a region-appropriate
            :class:`SafeCompletion` on a HARD verdict (locale-neutral when unknown).
        settings: The R1 knobs (defaults read from ``PERSONA_SAFETY_*``).

    Returns:
        The :class:`InterceptVerdict`. HARD carries a :class:`SafeCompletion`; SOFT
        carries the override directive; NONE carries neither.
    """
    try:
        resolved = settings if settings is not None else SafetyInterceptSettings()
        if not resolved.enabled:
            return InterceptVerdict(InterceptAction.NONE)
        text = _normalise(user_message)
        if not text:
            return InterceptVerdict(InterceptAction.NONE)
        # R1-hard first — the acute, explicit, high-precision path wins. The HARD
        # verdict ALSO carries the soft directive: a caller that has not yet wired
        # the out-of-band generation bypass still injects the escalation directive
        # (strictly safer than the bare R0 floor for a life-safety signal), while a
        # caller that HAS wired the bypass uses ``completion`` and ignores the
        # directive. The directive is the interim floor under the bypass, never a
        # substitute for it.
        if any(phrase in text for phrase in _HARD_PHRASES):
            return InterceptVerdict(
                InterceptAction.HARD,
                soft_directive=R1_SOFT_DIRECTIVE,
                completion=safe_completion(locale=locale),
            )
        if any(phrase in text for phrase in _SOFT_PHRASES):
            return InterceptVerdict(InterceptAction.SOFT, soft_directive=R1_SOFT_DIRECTIVE)
        return InterceptVerdict(InterceptAction.NONE)
    except Exception:  # noqa: BLE001 — fail-soft is the whole point: never crash a turn.
        return InterceptVerdict(InterceptAction.NONE)


# --- locale-aware safe completions (Group-B note 1) ------------------------------
#
# Conservative by design: a confidently-wrong hotline is its own harm, so only a
# locale we are sure of names a specific resource; everything else uses
# locale-neutral framing. ``116 123`` is the emotional-support helpline number used
# in Norway (the product's primary user base) and other European countries; ``113``
# is the Norwegian medical emergency number. Both renderings disclose AI-ness so a
# distressed user is never deceived about talking to a person.

_NORWAY_CHAT = (
    "I can hear that you're in a lot of pain right now, and I want to be honest with "
    "you: I'm an AI, so I'm not the person who can carry this with you. Please reach "
    "out to someone who can, right now. In Norway you can call Mental Helse on "
    "116 123, any time, day or night, and if you're in immediate danger call 113. "
    "You deserve real support, and you don't have to face this alone."
)
_NORWAY_VOICE = (
    "I can hear you're in real pain, and I want to be honest: I'm an AI, not someone "
    "who can carry this with you. Please reach out to a person right now. In Norway "
    "you can call 116 123, any time. If you're in danger, call 113. You don't have to "
    "face this alone."
)
_NEUTRAL_CHAT = (
    "I can hear that you're in a lot of pain right now, and I want to be honest with "
    "you: I'm an AI, so I'm not the person who can carry this with you. Please reach "
    "out to someone who can, right now — a crisis line where you are, or your local "
    "emergency number. You deserve real support, and you don't have to face this "
    "alone."
)
_NEUTRAL_VOICE = (
    "I can hear you're in real pain, and I want to be honest: I'm an AI, not someone "
    "who can carry this with you. Please reach out to a person right now, a crisis "
    "line where you are, or your local emergency number. You don't have to face this "
    "alone."
)

_LOCALE_COMPLETIONS: dict[str, SafeCompletion] = {
    "no": SafeCompletion(chat_text=_NORWAY_CHAT, voice_text=_NORWAY_VOICE),
}
_NEUTRAL_COMPLETION = SafeCompletion(chat_text=_NEUTRAL_CHAT, voice_text=_NEUTRAL_VOICE)


def safe_completion(locale: str | None) -> SafeCompletion:
    """The locale-appropriate R1-hard safe completion (Group-B note 1).

    A known locale names a region-appropriate resource; an unknown or absent locale
    uses locale-neutral framing rather than a confidently-wrong hotline. The locale
    is matched on its primary subtag (``"nb-NO"`` → ``"no"``... — case-insensitive).
    """
    if locale:
        primary = locale.lower().replace("_", "-").split("-", 1)[0]
        completion = _LOCALE_COMPLETIONS.get(primary)
        if completion is not None:
            return completion
    return _NEUTRAL_COMPLETION
