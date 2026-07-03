"""Tests for the R1 path-independent turn-time safety gate (Spec V11, V11-D-5).

R1 is the out-of-band override the B1 character-lock FLOOR backstops — because a
strong character-lock is adversarially biased against noticing crisis (the
mechanical-opposition finding), the wellbeing yield cannot rely on the model's own
judgment alone. R1 runs a lexical detector over the *current user message* (sub-ms,
no model call, no network → no TTFT hit), path-independent across chat and voice.

Precision split (V11-D-5):
- R1-hard (DEFAULT for acute explicit W1 — suicidal ideation): a tight, high-precision
  trigger → an out-of-band deterministic safe completion that BYPASSES persona
  generation. Locale-aware (Group-B note 1) and voice-aware (note 2).
- R1-soft (recall net for W2/W3 + ambiguous W1): a wider trigger → a high-salience
  conditional override directive injected into the prompt.

Fail-soft (Group-B note 3): disabled / erroring / empty-lexicon → the classifier
returns NONE so the caller falls back to the always-on R0 floor — NEVER to nothing.
"""

from __future__ import annotations

import pytest
from persona_runtime.prompt import PromptMode
from persona_runtime.safety_intercept import (
    SAFETY_INTERCEPT_VERSION,
    InterceptAction,
    SafetyInterceptSettings,
    classify_user_message,
    safe_completion,
)


class TestPrecisionSplit:
    def test_explicit_suicidal_ideation_is_hard(self) -> None:
        v = classify_user_message("i want to kill myself tonight")
        assert v.action is InterceptAction.HARD
        assert v.completion is not None

    def test_hard_also_carries_directive_as_interim_floor(self) -> None:
        """HARD carries the escalation directive too, so a caller that has not yet
        wired the out-of-band bypass still escalates (strictly safer than bare R0)."""
        v = classify_user_message("i want to kill myself tonight")
        assert v.action is InterceptAction.HARD
        assert v.soft_directive  # interim floor under the (next-step) bypass

    def test_broader_distress_is_soft(self) -> None:
        v = classify_user_message("honestly what's the point anymore, i can't go on")
        assert v.action is InterceptAction.SOFT
        assert v.soft_directive

    def test_reality_confusion_is_soft(self) -> None:
        v = classify_user_message("you're the only one who understands me, are you real?")
        assert v.action is InterceptAction.SOFT

    def test_benign_message_is_none(self) -> None:
        v = classify_user_message("what are my rights as a tenant?")
        assert v.action is InterceptAction.NONE
        assert v.soft_directive is None
        assert v.completion is None

    def test_explicit_takes_precedence_over_soft(self) -> None:
        """A message carrying both signals routes to HARD (the acute path wins)."""
        v = classify_user_message("i feel hopeless and i want to end my life")
        assert v.action is InterceptAction.HARD

    def test_hard_is_high_precision_not_literary(self) -> None:
        """A figurative 'kill for a coffee' must NOT trip the acute hard gate."""
        v = classify_user_message("ugh i could kill for a coffee right now")
        assert v.action is not InterceptAction.HARD


class TestSafeCompletionLocaleAware:
    def test_norway_locale_names_a_regional_resource(self) -> None:
        c = safe_completion(locale="no")
        assert "116 123" in c.chat_text  # the helpline used in Norway

    def test_unknown_locale_is_locale_neutral_no_foreign_number(self) -> None:
        c = safe_completion(locale="zz")
        # Locale-neutral framing, never a confidently-wrong hardcoded number.
        assert "116 123" not in c.chat_text
        assert "988" not in c.chat_text  # never the US-centric default
        assert "where you are" in c.chat_text.lower()

    def test_none_locale_falls_back_to_neutral(self) -> None:
        c = safe_completion(locale=None)
        assert "988" not in c.chat_text
        assert "where you are" in c.chat_text.lower()

    def test_completion_always_discloses_ai(self) -> None:
        """A distressed user must not be deceived about talking to an AI. The completion
        is now localized (R6-D-5): the ``no`` rendering discloses in Norwegian (``KI``),
        the neutral English ones in ``an AI``. Full per-language disclosure is covered by
        ``test_crisis_completions``."""
        assert "KI" in safe_completion(locale="no").chat_text  # Norwegian AI disclosure
        for loc in ("zz", None):
            assert "an AI" in safe_completion(locale=loc).chat_text


class TestSafeCompletionVoiceAware:
    def test_voice_variant_differs_from_chat_and_is_shorter(self) -> None:
        c = safe_completion(locale="no")
        assert c.voice_text != c.chat_text
        assert len(c.voice_text) < len(c.chat_text)

    def test_voice_variant_still_names_the_key_resource(self) -> None:
        assert "116 123" in safe_completion(locale="no").voice_text

    def test_render_picks_variant_by_mode(self) -> None:
        c = safe_completion(locale="no")
        assert c.render(PromptMode.VOICE) == c.voice_text
        assert c.render(PromptMode.CHAT) == c.chat_text


class TestFailSoftDegradesToR0:
    def test_disabled_returns_none_so_caller_uses_r0_floor(self) -> None:
        s = SafetyInterceptSettings(enabled=False)
        v = classify_user_message("i want to kill myself", settings=s)
        assert v.action is InterceptAction.NONE  # → caller falls back to the R0 floor

    def test_classifier_never_raises_on_odd_input(self) -> None:
        for bad in ("", "   ", "\n\n", "🙂", "x" * 100_000):
            assert classify_user_message(bad).action in {
                InterceptAction.NONE,
                InterceptAction.SOFT,
                InterceptAction.HARD,
            }


class TestVersioned:
    def test_version_constant_is_set(self) -> None:
        assert SAFETY_INTERCEPT_VERSION


@pytest.mark.parametrize("mode", [PromptMode.CHAT, PromptMode.VOICE])
def test_classify_is_path_independent_of_mode(mode: PromptMode) -> None:
    """The detector reads the user message only — identical verdict either path.

    Mode never enters classification; it only selects the completion *rendering*.
    """
    text = "i want to kill myself"
    verdict = classify_user_message(text)
    assert verdict.action is InterceptAction.HARD
    assert verdict.completion is not None
    # The same verdict renders per mode: the spoken variant on the voice path.
    rendered = verdict.completion.render(mode)
    assert rendered == (
        verdict.completion.voice_text if mode is PromptMode.VOICE else verdict.completion.chat_text
    )
