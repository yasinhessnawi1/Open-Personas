"""The versioned extraction prompt encodes §4 (Spec K2, T2; D-K2-3, Spec-10 discipline).

The prompt is the artifact T6's hard gate grades — so these tests pin that the
§4 judgement rules are present and that the frozen few-shot examples are themselves
well-formed (they round-trip through the parser) and respect the two safety bars:
means-redaction (D-K2-7) and grounded-not-inferred (no speculative diagnosis).
"""

from __future__ import annotations

import pytest
from persona.extraction import ExtractionInput, InteractionKind
from persona.graph.models import LinkType
from persona.wellbeing import WellbeingCategory
from persona_runtime.extraction.parse import parse_candidates
from persona_runtime.extraction.prompt import (
    EXAMPLE_CAUSATION_TRAP_OUTPUT,
    EXAMPLE_MEANS_REDACTION_OUTPUT,
    EXAMPLE_RICH_OUTPUT,
    EXAMPLE_SMALL_TALK_OUTPUT,
    EXAMPLE_SPECULATION_OUTPUT,
    EXAMPLE_STATED_CAUSATION_OUTPUT,
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_messages,
)


def test_prompt_version_is_pinned_at_v3() -> None:
    # Bumped for the K12-T5 concise-concept-name rule (Spec-10 versioning); T6's
    # hard gate now grades v3.
    assert EXTRACTION_PROMPT_VERSION == "v3"


def test_system_prompt_encodes_the_section_4_rules() -> None:
    p = EXTRACTION_SYSTEM_PROMPT.lower()
    # grounded-over-inferred plus a verbatim evidence span
    assert "evidence_span" in EXTRACTION_SYSTEM_PROMPT
    assert "verbatim" in p
    # the decline list forbids inference and diagnosis
    assert "diagnos" in p
    # restraint and volume anchoring (the "0-3 typical" anchor)
    assert "0" in p
    assert "3" in p
    # conservative causation enforces the K0 causal bar
    assert "causation" in p or "because" in p
    # the self-harm means must be redacted, never stored
    assert "means" in p
    # the structured JSON output contract
    assert "candidates" in EXTRACTION_SYSTEM_PROMPT
    # T4: the temporal/causal relation contract
    assert "proposed_relations" in EXTRACTION_SYSTEM_PROMPT
    assert "temporal" in p
    assert "causal" in p


# --- K12, T5: concise concept_name labels (the graph's node label) ---


def test_system_prompt_instructs_a_concise_1_to_3_word_concept_name() -> None:
    p = EXTRACTION_SYSTEM_PROMPT.lower()
    assert "1-3 word" in p or "1 to 3 word" in p
    # NOT a truncation of content — the label is a separate, short paraphrase.
    assert "truncation" in p
    assert "never empty" in p


@pytest.mark.parametrize(
    "example",
    [
        EXAMPLE_RICH_OUTPUT,
        EXAMPLE_SPECULATION_OUTPUT,
        EXAMPLE_MEANS_REDACTION_OUTPUT,
        EXAMPLE_STATED_CAUSATION_OUTPUT,
        EXAMPLE_CAUSATION_TRAP_OUTPUT,
    ],
)
def test_few_shot_concept_names_are_at_most_three_words(example: str) -> None:
    # The frozen few-shot examples ARE the spec-by-example (K12-T5): every
    # concept_name they show the model must itself respect the 1-3 word guide,
    # and none may be a verbatim prefix of its own content.
    for candidate in parse_candidates(example):
        words = candidate.concept_name.split()
        assert 1 <= len(words) <= 3, candidate.concept_name
        assert not candidate.content.startswith(candidate.concept_name)


def test_system_prompt_lists_the_five_wellbeing_categories() -> None:
    for cat in WellbeingCategory:
        assert cat.value in EXTRACTION_SYSTEM_PROMPT


def test_few_shot_examples_are_well_formed_and_parse() -> None:
    # The curated example outputs are the spec-by-example; they must be valid.
    spec = parse_candidates(EXAMPLE_SPECULATION_OUTPUT)
    assert len(spec) >= 1  # the grounded struggle IS captured
    means = parse_candidates(EXAMPLE_MEANS_REDACTION_OUTPUT)
    assert len(means) >= 1


def test_small_talk_example_extracts_nothing() -> None:
    assert parse_candidates(EXAMPLE_SMALL_TALK_OUTPUT) == ()


def test_speculation_example_captures_the_struggle_not_a_diagnosis() -> None:
    cands = parse_candidates(EXAMPLE_SPECULATION_OUTPUT)
    blob = " ".join(c.concept_name + " " + c.content for c in cands).lower()
    # the grounded focus-struggle is captured; the inferred clinical label is NOT
    assert "focus" in blob or "concentrat" in blob
    assert "adhd" not in blob


def test_means_redaction_example_tags_self_harm_and_omits_the_means() -> None:
    cands = parse_candidates(EXAMPLE_MEANS_REDACTION_OUTPUT)
    assert any(c.wellbeing_category is WellbeingCategory.SELF_HARM for c in cands)
    # the means token from the disclosure must not survive into ANY field
    fields = " ".join(
        c.concept_name + " " + c.content + " " + c.evidence_span for c in cands
    ).lower()
    assert "pills" not in fields
    assert "overdose" not in fields


# --- T4: conservative-causation examples (criterion 4 — the DECLINE is the proof) ---


def test_stated_causation_example_asserts_a_causal_link() -> None:
    cands = parse_candidates(EXAMPLE_STATED_CAUSATION_OUTPUT)
    rels = [r for c in cands for r in c.proposed_relations]
    assert any(r.link_type is LinkType.CAUSAL for r in rels)


def test_causation_trap_example_declines_the_causal_link() -> None:
    # Temporal adjacency without a stated cause MUST NOT become a causal link.
    # The decline is the proof of criterion 4, not just the assertion.
    cands = parse_candidates(EXAMPLE_CAUSATION_TRAP_OUTPUT)
    rels = [r for c in cands for r in c.proposed_relations]
    assert all(r.link_type is not LinkType.CAUSAL for r in rels)


def test_build_messages_places_system_then_interaction_content() -> None:
    msgs = build_extraction_messages(
        ExtractionInput(
            interaction_kind=InteractionKind.CONVERSATION,
            interaction_id="c1",
            persona_id="p1",
            content="USER: I just adopted a rescue dog named Pixel.",
        )
    )
    assert msgs[0].role == "system"
    assert msgs[0].content == EXTRACTION_SYSTEM_PROMPT
    assert msgs[-1].role == "user"
    assert "Pixel" in msgs[-1].content
