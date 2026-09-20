"""Unit tests for the versioned authoring prompt (spec 10, T01, §3 / D-10-4).

No DB, no model. Covers: the §3.2 tool/skill injection substitution, the
message roles/order for both the authoring and refinement prompts, and the
guard that both few-shot example personas themselves validate as v1.0 (so they
can't silently drift invalid — the cross-model compliance lever, S10-2).
"""

from __future__ import annotations

import yaml
from persona.schema.persona import Persona
from persona.schema.safety import SAFETY_CONSTRAINT
from persona_api.services.authoring_prompt import (
    AUTHORING_PROMPT_VERSION,
    EXAMPLE_COMPLEX_YAML,
    EXAMPLE_SIMPLE_YAML,
    QUESTIONS_MARKER,
    build_authoring_prompt,
    build_refinement_prompt,
)

_TOOLS = ["web_search", "web_fetch"]
_SKILLS = ["web_research", "document_drafting"]


def test_prompt_version_is_set() -> None:
    assert isinstance(AUTHORING_PROMPT_VERSION, str)
    assert AUTHORING_PROMPT_VERSION


def test_prompt_version_is_v5() -> None:
    # Bumped v4 -> v5 for R9-155: the prompt now asks the author to decide the
    # persona's presentation, and both few-shots carry it (Sage synthetic,
    # Astrid human). A bump means the paid corpus eval should be re-measured.
    assert AUTHORING_PROMPT_VERSION == "v5"


def test_naming_instruction_forbids_few_shot_and_placeholder_names() -> None:
    system = build_authoring_prompt("a cooking assistant", _TOOLS, _SKILLS)[0].content
    # the prompt counters the few-shot anchoring directly
    assert "NAMING" in system
    # the few-shot names are named as banned, not just shown as examples
    assert "Sage" in system
    assert "Astrid" in system
    # a sample of the banned generic AI placeholders is explicitly listed
    for placeholder in ("Alex", "Aria", "Nova", "Luna"):
        assert placeholder in system
    # the language-fit instruction is present
    assert "language_default" in system


def test_prompt_carries_the_shared_safety_constraint_verbatim() -> None:
    # D-36-safety-constant: the prompt interpolates the single SAFETY_CONSTRAINT
    # source of truth rather than a hand-copied literal — guards drift between
    # the instruction and the enforcement floor.
    system = build_authoring_prompt("a cooking assistant", _TOOLS, _SKILLS)[0].content
    assert SAFETY_CONSTRAINT in system


def test_few_shot_examples_lead_with_the_safety_constraint() -> None:
    # The two few-shots model the constraint as the FIRST constraint; if either
    # drifts, the cross-model compliance lever stops teaching it.
    for example in (EXAMPLE_SIMPLE_YAML, EXAMPLE_COMPLEX_YAML):
        constraints = yaml.safe_load(example)["identity"]["constraints"]
        assert constraints[0] == SAFETY_CONSTRAINT


def test_build_authoring_prompt_injects_tools_and_skills() -> None:
    msgs = build_authoring_prompt("a cooking assistant", _TOOLS, _SKILLS)
    assert [m.role for m in msgs] == ["system", "user"]
    system = msgs[0].content
    # placeholders are substituted (no literal leftovers)
    assert "[AVAILABLE_TOOLS]" not in system
    assert "[AVAILABLE_SKILLS]" not in system
    assert "- web_search" in system
    assert "- web_research" in system
    assert msgs[1].content == "a cooking assistant"


def test_build_authoring_prompt_empty_catalog_says_none() -> None:
    msgs = build_authoring_prompt("x", [], [])
    system = msgs[0].content
    assert "[AVAILABLE_TOOLS]" not in system
    assert "(none available)" in system


def test_authoring_prompt_states_the_questions_marker() -> None:
    system = build_authoring_prompt("x", _TOOLS, _SKILLS)[0].content
    assert QUESTIONS_MARKER in system


def test_build_refinement_prompt_message_sequence() -> None:
    msgs = build_refinement_prompt(
        current_yaml=EXAMPLE_SIMPLE_YAML,
        question="Which cuisine?",
        answer="Italian.",
        available_tools=_TOOLS,
        available_skills=_SKILLS,
    )
    assert [m.role for m in msgs] == ["system", "user", "assistant", "user", "user"]
    assert "Which cuisine?" in msgs[2].content
    assert msgs[3].content == "Italian."
    assert EXAMPLE_SIMPLE_YAML in msgs[1].content
    # refinement re-injects the catalog so it still only suggests real tools
    assert "[AVAILABLE_TOOLS]" not in msgs[0].content


def _validate(yaml_text: str) -> Persona:
    raw = yaml.safe_load(yaml_text)
    assert isinstance(raw, dict)
    raw.setdefault("persona_id", "draft")
    raw.setdefault("owner_id", "draft")
    return Persona.model_validate(raw)


def test_few_shot_simple_example_validates_as_v1() -> None:
    p = _validate(EXAMPLE_SIMPLE_YAML)
    # the cross-model levers the prompt teaches: a safety constraint + epistemic diversity
    assert any("fabricate" in c.lower() for c in p.identity.constraints)
    assert any(w.epistemic != "fact" for w in p.worldview)


def test_the_two_few_shots_show_one_form_each(  # R9-155
) -> None:
    """The decisive compliance lever, per this module's own note about few-shots.

    Prose says a persona may be synthetic; an example SHOWS it. With both
    examples human, a floor model would learn that `form: human` is what this
    field is for and quietly never emit the other branch, which is exactly the
    failure R9-155 is about. Sage is the synthetic one ("It favours simple
    techniques" is how its own background already reads) and Astrid the human
    one, so both branches are demonstrated at zero extra prompt length.
    """
    sage = _validate(EXAMPLE_SIMPLE_YAML).identity.presentation
    astrid = _validate(EXAMPLE_COMPLEX_YAML).identity.presentation
    assert sage is not None
    assert astrid is not None
    assert sage.form == "synthetic"
    assert astrid.form == "human"
    # And one of each kind of `presents`: a declared gender, and the honest
    # refusal to declare one.
    assert sage.presents == "unspecified"
    assert astrid.presents == "feminine"
    # Neither invents an appearance; nothing in either description says what
    # the persona looks like.
    assert sage.appearance is None
    assert astrid.appearance is None


def test_the_prompt_tells_the_author_not_to_read_presentation_off_the_name() -> None:
    """D-29-1's surviving rule, carried into the one place that still infers.

    Both halves are asserted separately, not as an `or` over two phrasings: the
    rule is stated once for `form` and once for `presents` because they are two
    different inferences, and an `or` would go on passing after either one was
    deleted.
    """
    system = build_authoring_prompt("a cooking assistant", _TOOLS, _SKILLS)[0].content
    assert "presentation" in system
    # `presents` must not be read off the invented name.
    assert "infer it from the name you just invented" in system
    # Neither must `form`.
    assert "never from its name" in system


def test_the_schema_block_and_the_examples_agree_about_presentation() -> None:
    """A field the schema permits but no example shows is a field models omit."""
    system = build_authoring_prompt("a cooking assistant", _TOOLS, _SKILLS)[0].content
    assert "form: <human | synthetic>" in system
    assert "form: synthetic" in system  # via the Sage example
    assert "form: human" in system  # via the Astrid example


def test_few_shot_complex_example_validates_as_v1() -> None:
    p = _validate(EXAMPLE_COMPLEX_YAML)
    assert p.identity.language_default == "nb"
    assert any("legal advice" in c.lower() for c in p.identity.constraints)
    assert any(w.epistemic != "fact" for w in p.worldview)
    # only suggests tools/skills that exist in the catalog
    assert set(p.tools) <= {"web_search", "web_fetch", "file_read", "file_write"}
    assert set(p.skills) <= {"web_research", "document_drafting"}
