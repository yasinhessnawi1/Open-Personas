"""Persona name-parsing / addressing — conservative, precision-over-recall (C1 T7, D-C1-4).

The sticky-active pointer does the routing; the parser only detects a SWITCH. So it
optimizes for precision: a missed address is harmless (routes to the active persona),
a false switch is the only real cost. Rules (stdlib ``re``, Unicode-by-default):

- only the LEADING or TRAILING position is inspected (never mid-sentence — the
  "every *max* in a sentence" class is eliminated structurally);
- a trailing name needs a preceding vocative comma/colon (so "what's the max" does
  NOT address persona Max); a leading name is the deliberate address convention;
- EXACT whole-word, case-insensitive, Unicode-aware (matches Søren/김; not maximum);
- multiple distinct personas matched → Ambiguous (the flow stays on the active
  persona rather than guess).

The no-op-on-re-naming-active rule lives downstream (the T6 ``decide_foreground``);
the parser only says WHICH persona is named. Owned surface — api-free.
"""

from __future__ import annotations

from persona_connectors.domain.addressing import (
    Addressed,
    Ambiguous,
    NoName,
    parse_addressed_persona,
    resolve_persona_tag,
)

_PERSONAS = {"astrid": ["Astrid"], "kai": ["Kai", "K"]}


def test_leading_name_addresses_the_persona() -> None:
    assert parse_addressed_persona("Astrid, how are you?", persona_names=_PERSONAS) == Addressed(
        persona_id="astrid"
    )


def test_leading_name_without_comma_still_addresses() -> None:
    """Starting a message with the name is the deliberate address convention."""
    assert parse_addressed_persona("Astrid how are you", persona_names=_PERSONAS) == Addressed(
        persona_id="astrid"
    )


def test_just_the_name_addresses() -> None:
    assert parse_addressed_persona("Astrid", persona_names=_PERSONAS) == Addressed(
        persona_id="astrid"
    )


def test_trailing_name_with_vocative_comma_addresses() -> None:
    assert parse_addressed_persona("what do you think, Kai?", persona_names=_PERSONAS) == Addressed(
        persona_id="kai"
    )


def test_case_insensitive() -> None:
    assert parse_addressed_persona("ASTRID hi", persona_names=_PERSONAS) == Addressed(
        persona_id="astrid"
    )


def test_alias_addresses_the_persona() -> None:
    assert parse_addressed_persona("K, hello", persona_names=_PERSONAS) == Addressed(
        persona_id="kai"
    )


def test_no_name_returns_noname() -> None:
    assert parse_addressed_persona("how are you today?", persona_names=_PERSONAS) == NoName()


def test_mid_sentence_name_is_not_an_address() -> None:
    """A name in the middle is not addressing (the 'the max value' class)."""
    assert parse_addressed_persona("tell Astrid I said hi", persona_names=_PERSONAS) == NoName()


def test_trailing_name_without_comma_is_not_an_address() -> None:
    """'what's the max' must NOT address persona Max (no vocative comma)."""
    assert parse_addressed_persona("what's the max", persona_names={"max": ["Max"]}) == NoName()


def test_substring_does_not_match_whole_word() -> None:
    """'maximum'/'Annabelle' must not match Max/Anna (whole-word boundary)."""
    assert parse_addressed_persona("the maximum value", persona_names={"max": ["Max"]}) == NoName()
    assert parse_addressed_persona("Annabelle, hi", persona_names={"anna": ["Anna"]}) == NoName()


def test_unicode_names_match_leading() -> None:
    """Multilingual: Nordic + CJK names match at the leading position (Unicode-aware)."""
    assert parse_addressed_persona("Søren, hjelp", persona_names={"s": ["Søren"]}) == Addressed(
        persona_id="s"
    )
    assert parse_addressed_persona("김, 안녕", persona_names={"k": ["김"]}) == Addressed(
        persona_id="k"
    )


def test_two_personas_matched_is_ambiguous() -> None:
    """Leading one + trailing another → ambiguous; the flow stays on the active persona."""
    result = parse_addressed_persona("Astrid, Kai", persona_names=_PERSONAS)
    assert isinstance(result, Ambiguous)
    assert set(result.candidate_persona_ids) == {"astrid", "kai"}


# --- Spec C5 D-C5-3: envelope persona-tag resolution (plus-addressing) ---


def test_resolve_persona_tag_unique_match() -> None:
    """A plus-address tag resolves to the one persona whose name-slug it equals."""
    assert resolve_persona_tag("astrid", persona_names=_PERSONAS) == Addressed(persona_id="astrid")
    assert resolve_persona_tag("Kai", persona_names=_PERSONAS) == Addressed(persona_id="kai")


def test_resolve_persona_tag_slug_normalises_case_and_punctuation() -> None:
    """Casefold + alphanumeric-only slug: 'drhansen' matches 'Dr. Hansen'."""
    names = {"h": ["Dr. Hansen"], "kai": ["Kai"]}
    assert resolve_persona_tag("drhansen", persona_names=names) == Addressed(persona_id="h")
    caps = resolve_persona_tag("ASTRID", persona_names={"a": ["Astrid"]})
    assert caps == Addressed(persona_id="a")


def test_resolve_persona_tag_unknown_or_empty_is_noname() -> None:
    """An unknown/empty tag → NoName (the flow then falls back to text-parse)."""
    assert resolve_persona_tag("nobody", persona_names=_PERSONAS) == NoName()
    assert resolve_persona_tag("", persona_names=_PERSONAS) == NoName()


def test_resolve_persona_tag_two_matches_is_ambiguous() -> None:
    """Two personas sharing the tag slug → Ambiguous (the flow falls back, never guesses)."""
    result = resolve_persona_tag("astrid", persona_names={"a1": ["Astrid"], "a2": ["astrid"]})
    assert isinstance(result, Ambiguous)
    assert set(result.candidate_persona_ids) == {"a1", "a2"}


def test_resolve_persona_tag_matches_only_within_passed_names() -> None:
    """The RLS discipline (pure half): the tag resolves ONLY within the passed map.
    Two owners' maps each carry an 'Astrid' under DIFFERENT ids — resolving against one
    map never sees the other's persona (the flow passes an owner-scoped map)."""
    owner_a = {"astrid_a": ["Astrid"], "kai_a": ["Kai"]}
    owner_b = {"astrid_b": ["Astrid"]}
    assert resolve_persona_tag("astrid", persona_names=owner_a) == Addressed(persona_id="astrid_a")
    assert resolve_persona_tag("astrid", persona_names=owner_b) == Addressed(persona_id="astrid_b")
