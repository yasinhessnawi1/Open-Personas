"""T7 — localized SafeCompletions + verified crisis numbers (Spec R6, R6-D-5).

The real-world-harm task. Asserts:

- **Per-language selection** — each covered locale renders its OWN native completion
  (chat + voice), distinct across languages, matched on the primary subtag.
- **Structural completeness** — non-empty, voice shorter than chat, AI-disclosure present
  in-language, and a resource present (a national number for nb/sv/da, the international
  pointer for ar/tr/ur + neutral).
- **Locale ≠ country (hard rule)** — ar/tr/ur carry NO national number (they span many
  countries) and point to findahelpline.com; only country-mapped locales name a number.
- **Numbers come ONLY from the provenance table** — a structural guard scans every
  completion for digit-runs and asserts each is a number declared in ``crisis_resources``.
  A hardcoded/hallucinated number anywhere in a template fails the build.
- **Fail-soft** — an unknown locale degrades to English + pointer, never silence.
- **Provenance integrity** — every resource has a source URL + verified date; a helpline
  implies a known country; a pointer-only locale declares no country/number.

The numbers themselves are re-verified by a human against ``source_url`` (the table makes
that pass cheap); the cadence lives in docs/MAINTENANCE.md.
"""

from __future__ import annotations

import re

import pytest
from _crisis_encoder_eval import COVERED_NONENGLISH_LANGS  # type: ignore[import-not-found]
from persona_runtime.prompt import PromptMode
from persona_runtime.safety_intercept import (
    FINDAHELPLINE,
    crisis_resources,
    safe_completion,
)

# Disclosure token each language uses to name its AI-ness (never deceive the user).
_AI_TOKEN = {
    "nb": "KI",
    "no": "KI",
    "sv": "AI",
    "da": "AI",
    "ar": "ذكاء اصطناعي",
    "tr": "yapay zekâ",
    "ur": "AI",
}
_POINTER_FIRST = ("ar", "tr", "ur")
_COUNTRY_MAPPED = ("nb", "sv", "da")


def _all_numbers() -> set[str]:
    """Every crisis number in the provenance table, whitespace-normalised."""
    nums: set[str] = set()
    for r in crisis_resources():
        for n in (r.helpline, r.emergency):
            if n:
                nums.add(re.sub(r"\s+", "", n))
    return nums


def _digit_runs(text: str) -> list[str]:
    """Every phone-number-like digit run in ``text``, whitespace-normalised."""
    return [re.sub(r"\s+", "", m) for m in re.findall(r"\d[\d ]*\d|\d", text)]


class TestPerLanguageSelection:
    def test_each_covered_language_renders_its_own_completion(self) -> None:
        seen: set[str] = set()
        for lang in COVERED_NONENGLISH_LANGS:
            c = safe_completion(locale=lang)
            assert c.chat_text.strip()
            assert _AI_TOKEN[lang] in c.chat_text, f"{lang}: AI disclosure missing"
            seen.add(c.chat_text)
        assert len(seen) == len(COVERED_NONENGLISH_LANGS), "languages share a completion"

    def test_primary_subtag_and_region_variants_match(self) -> None:
        assert safe_completion("nb-NO").chat_text == safe_completion("nb").chat_text
        assert safe_completion("ar-EG").chat_text == safe_completion("ar").chat_text
        # Norwegian Bokmål may arrive as the macrolanguage ``no``.
        assert safe_completion("no").chat_text == safe_completion("nb").chat_text

    def test_voice_variant_is_shorter_and_present_per_language(self) -> None:
        for lang in (*COVERED_NONENGLISH_LANGS, "no", "en"):
            c = safe_completion(locale=lang)
            assert c.voice_text.strip()
            assert len(c.voice_text) < len(c.chat_text), f"{lang}: voice not shorter"
            assert c.render(PromptMode.VOICE) == c.voice_text


class TestLocaleIsNotCountry:
    def test_pointer_first_locales_name_no_national_number(self) -> None:
        # ar/tr/ur span many countries → NO number may appear; must point to the directory.
        for lang in _POINTER_FIRST:
            c = safe_completion(locale=lang)
            for text in (c.chat_text, c.voice_text):
                assert not _digit_runs(text), f"{lang}: a number leaked into a pointer locale"
                assert FINDAHELPLINE in text, f"{lang}: no findahelpline pointer"

    def test_country_mapped_locales_name_their_number(self) -> None:
        for lang in _COUNTRY_MAPPED:
            r = next(x for x in crisis_resources() if x.locale == lang)
            assert r.helpline is not None
            assert re.sub(r"\s+", "", r.helpline) in _digit_runs(
                safe_completion(locale=lang).chat_text
            ), f"{lang}: helpline not rendered"


class TestNumbersComeOnlyFromTheTable:
    def test_no_completion_contains_a_number_absent_from_the_table(self) -> None:
        allowed = _all_numbers()
        locales = (*COVERED_NONENGLISH_LANGS, "no", "en", None, "zz", "fr")
        for loc in locales:
            c = safe_completion(locale=loc)
            for text in (c.chat_text, c.voice_text):
                for run in _digit_runs(text):
                    assert run in allowed, f"locale {loc!r}: number {run!r} not in the table"


class TestFailSoft:
    def test_unknown_locale_degrades_to_english_pointer_never_silence(self) -> None:
        for loc in ("fr", "zz", "xx-YY", None):
            c = safe_completion(locale=loc)
            assert c.chat_text.strip()
            assert c.voice_text.strip()
            assert "an AI" in c.chat_text  # English neutral
            assert FINDAHELPLINE in c.chat_text
            assert not _digit_runs(c.chat_text)  # never a confidently-wrong foreign number

    def test_neutral_completion_names_no_foreign_national_number(self) -> None:
        c = safe_completion(locale="zz")
        assert "116 123" not in c.chat_text
        assert "988" not in c.chat_text  # never the US-centric default


class TestProvenanceIntegrity:
    def test_every_resource_has_source_and_verified_date(self) -> None:
        for r in crisis_resources():
            assert r.source_url.startswith("https://"), f"{r.locale}: no source URL"
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.verified), f"{r.locale}: bad date"
            assert r.note, f"{r.locale}: no provenance note"

    def test_a_helpline_implies_a_known_country(self) -> None:
        for r in crisis_resources():
            if r.helpline is not None:
                assert r.country is not None, f"{r.locale}: number without a known country"
                assert r.org is not None

    def test_pointer_first_locales_declare_no_country_or_number(self) -> None:
        for r in crisis_resources():
            if r.locale in _POINTER_FIRST:
                assert r.country is None
                assert r.helpline is None
                assert r.emergency is None
                assert r.source_url == f"https://{FINDAHELPLINE}"

    def test_table_covers_all_six_languages(self) -> None:
        locales = {r.locale for r in crisis_resources()}
        for lang in COVERED_NONENGLISH_LANGS:
            assert lang in locales

    def test_resources_are_immutable(self) -> None:
        r = crisis_resources()[0]
        with pytest.raises((AttributeError, TypeError)):
            r.helpline = "999"  # type: ignore[misc]  # frozen — numbers can't be mutated at runtime
