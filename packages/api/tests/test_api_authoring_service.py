"""Unit tests for the authoring draft generator + retry loop (spec 10, T03).

No DB, no real model — a scripted backend returns canned contents. The retry
loop is the *model-agnosticism mechanism* (D-10-3), so it is tested hard:
bad-YAML-then-good-YAML must fire the retry, feed the errors back, and succeed;
bad-then-bad must return a best-effort draft + errors without raising.
"""

from __future__ import annotations

import pytest
import yaml
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.persona import Persona
from persona_api.services.authoring_prompt import AUTHORING_PROMPT_VERSION
from persona_api.services.authoring_service import (
    AuthoringSampling,
    _validate_yaml,  # noqa: PLC2701 — the retry loop's own gate, tested directly
    generate_authoring_draft,
    refine_authoring_draft,
)

GOOD_YAML = """\
schema_version: "1.0"
identity:
  name: Lex
  role: Legal information assistant
  background: A general legal-information assistant for everyday questions.
  constraints:
    - Do not fabricate information; say when you don't know."""

# Top-level `hobbies` is not in the schema -> extra="forbid" rejects it.
BAD_YAML = GOOD_YAML + "\nhobbies:\n  - chess"

GOOD_WITH_QUESTIONS = (
    GOOD_YAML + '\n---QUESTIONS---\n[{"section": "identity", "question": "Which legal area?"}]'
)


class _ScriptedBackend:
    """A minimal ChatBackend stand-in: returns canned contents, records calls."""

    def __init__(self, *contents: str) -> None:
        self._contents = list(contents)
        self.calls: list[list] = []
        #: Sampling kwargs captured per chat() call (temperature/top_p/top_k).
        self.sampling: list[dict[str, object]] = []

    async def chat(self, messages: list, **kwargs: object) -> ChatResponse:
        self.calls.append(list(messages))
        self.sampling.append(
            {
                "temperature": kwargs.get("temperature"),
                "top_p": kwargs.get("top_p"),
                "top_k": kwargs.get("top_k"),
            }
        )
        content = self._contents[len(self.calls) - 1]
        return ChatResponse(
            content=content,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="mock",
            provider="mock",
            latency_ms=0.0,
        )


def _validates(yaml_text: str) -> bool:
    raw = yaml.safe_load(yaml_text)
    raw.setdefault("persona_id", "d")
    raw.setdefault("owner_id", "d")
    Persona.model_validate(raw)
    return True


@pytest.mark.asyncio
async def test_first_attempt_valid_no_retry() -> None:
    backend = _ScriptedBackend(GOOD_YAML)
    draft = await generate_authoring_draft(backend, "lawyer", [], [])  # type: ignore[arg-type]
    assert draft.errors is None
    assert len(backend.calls) == 1
    assert _validates(draft.yaml)
    assert draft.prompt_version == AUTHORING_PROMPT_VERSION


@pytest.mark.asyncio
async def test_invalid_then_valid_retry_fires_and_succeeds() -> None:
    # THE model-agnosticism proof: a weak model emits an invalid field first,
    # the retry feeds the error back, the second attempt validates.
    backend = _ScriptedBackend(BAD_YAML, GOOD_YAML)
    draft = await generate_authoring_draft(backend, "lawyer", [], [])  # type: ignore[arg-type]
    assert len(backend.calls) == 2
    assert draft.errors is None
    assert _validates(draft.yaml)


@pytest.mark.asyncio
async def test_retry_feeds_validation_errors_back() -> None:
    backend = _ScriptedBackend(BAD_YAML, GOOD_YAML)
    await generate_authoring_draft(backend, "lawyer", [], [])  # type: ignore[arg-type]
    retry_messages = backend.calls[1]
    # the retry includes the model's prior output + a user message with the errors
    assert retry_messages[-1].role == "user"
    feedback = retry_messages[-1].content
    assert "validation errors" in feedback.lower()
    assert "hobbies" in feedback  # the offending extra field is named back to the model


@pytest.mark.asyncio
async def test_retry_exhausted_returns_best_effort_with_errors() -> None:
    backend = _ScriptedBackend(BAD_YAML, BAD_YAML)
    draft = await generate_authoring_draft(backend, "lawyer", [], [])  # type: ignore[arg-type]
    assert len(backend.calls) == 2
    assert draft.errors is not None
    assert any("hobbies" in e for e in draft.errors)
    # no raise: the best-effort YAML is handed back for the form to fix


@pytest.mark.asyncio
async def test_questions_are_parsed_through() -> None:
    backend = _ScriptedBackend(GOOD_WITH_QUESTIONS)
    draft = await generate_authoring_draft(backend, "lawyer", [], [])  # type: ignore[arg-type]
    assert [q.section for q in draft.questions] == ["identity"]
    assert draft.questions[0].question == "Which legal area?"


@pytest.mark.asyncio
async def test_refine_threads_question_and_answer() -> None:
    backend = _ScriptedBackend(GOOD_YAML)
    draft = await refine_authoring_draft(
        backend,  # type: ignore[arg-type]
        current_yaml=GOOD_YAML,
        question="Which legal area?",
        answer="Tenancy law.",
        available_tools=[],
        available_skills=[],
    )
    assert draft.errors is None
    sent = backend.calls[0]
    # the refinement prompt carries the question + the user's answer
    assert any("Which legal area?" in m.content for m in sent)
    assert any(m.content == "Tenancy law." for m in sent)


# -- creative-draft vs deterministic-retry sampling split (D-10-3) -----------


@pytest.mark.asyncio
async def test_first_attempt_samples_at_creative_sampling() -> None:
    backend = _ScriptedBackend(GOOD_YAML)
    sampling = AuthoringSampling(temperature=0.9, top_p=0.95, top_k=60)
    await generate_authoring_draft(backend, "lawyer", [], [], sampling=sampling)  # type: ignore[arg-type]
    # the single creative attempt carries the configured sampling
    assert backend.sampling[0] == {"temperature": 0.9, "top_p": 0.95, "top_k": 60}


@pytest.mark.asyncio
async def test_default_sampling_is_creative() -> None:
    # No explicit sampling -> the AuthoringSampling default (creative temp 0.9).
    backend = _ScriptedBackend(GOOD_YAML)
    await generate_authoring_draft(backend, "lawyer", [], [])  # type: ignore[arg-type]
    assert backend.sampling[0]["temperature"] == 0.9


@pytest.mark.asyncio
async def test_retry_is_always_deterministic_even_when_first_is_creative() -> None:
    # THE D-10-3 split: first attempt samples hot, the validation-repair retry
    # is greedy (temperature 0.0, no top_p/top_k) so it reliably fixes schema.
    backend = _ScriptedBackend(BAD_YAML, GOOD_YAML)
    sampling = AuthoringSampling(temperature=0.9, top_p=0.95, top_k=60)
    draft = await generate_authoring_draft(
        backend,  # type: ignore[arg-type]
        "lawyer",
        [],
        [],
        sampling=sampling,
    )
    assert len(backend.sampling) == 2
    assert backend.sampling[0] == {"temperature": 0.9, "top_p": 0.95, "top_k": 60}
    assert backend.sampling[1] == {"temperature": 0.0, "top_p": None, "top_k": None}
    assert draft.errors is None


@pytest.mark.asyncio
async def test_refine_retry_is_deterministic() -> None:
    backend = _ScriptedBackend(BAD_YAML, GOOD_YAML)
    sampling = AuthoringSampling(temperature=0.8, top_p=None, top_k=None)
    await refine_authoring_draft(
        backend,  # type: ignore[arg-type]
        current_yaml=GOOD_YAML,
        question="Which legal area?",
        answer="Tenancy law.",
        available_tools=[],
        available_skills=[],
        sampling=sampling,
    )
    assert backend.sampling[0]["temperature"] == 0.8
    assert backend.sampling[1] == {"temperature": 0.0, "top_p": None, "top_k": None}


def test_authoring_sampling_is_frozen_and_strict() -> None:
    s = AuthoringSampling()
    with pytest.raises(Exception):  # noqa: B017,PT011 — frozen mutation rejected
        s.temperature = 0.1  # type: ignore[misc]
    with pytest.raises(Exception):  # noqa: B017,PT011 — extra="forbid"
        AuthoringSampling(bogus=1)  # type: ignore[call-arg]


# ----- R9-155: presentation through the real validate/retry loop -------------


_NO_PRESENTATION_YAML = GOOD_YAML
_BAD_PRESENTATION_YAML = """\
schema_version: "1.0"
identity:
  name: Lex
  role: Legal information assistant
  background: A general legal-information assistant for everyday questions.
  presentation:
    form: robot
    presents: female
  constraints:
    - Do not fabricate information; say when you don't know."""


def test_a_model_that_omits_presentation_still_produces_a_valid_persona() -> None:
    """Silence is honest. It must not be rescued with a default.

    A model that says nothing about presentation yields `None`, which means
    "not stated" and gives this persona exactly the avatar and voice behaviour
    it would have had before the field existed. Defaulting to `human` here
    would be the system guessing, which is the bug, not the fix.
    """
    assert _validate_yaml(_NO_PRESENTATION_YAML) == []
    raw = yaml.safe_load(_NO_PRESENTATION_YAML)
    raw["persona_id"] = raw["owner_id"] = "draft"
    assert Persona.model_validate(raw).identity.presentation is None


def test_an_invalid_presentation_is_fed_back_to_the_model_by_name() -> None:
    """The repair retry turns a bad value into a self-correction, for free.

    The errors this returns are exactly what `_retry_feedback` puts in front of
    the model, so they have to name the offending path rather than just saying
    the document is invalid.
    """
    errors = _validate_yaml(_BAD_PRESENTATION_YAML)
    assert errors
    assert any("presentation.form" in e for e in errors)
    assert any("presentation.presents" in e for e in errors)


def test_appearance_on_a_synthetic_persona_is_reported_actionably() -> None:
    """The one cross-field rule, and the message a model has to act on."""
    yaml_text = _NO_PRESENTATION_YAML.replace(
        "  constraints:",
        "  presentation:\n"
        "    form: synthetic\n"
        '    appearance: "a tall man in his forties"\n'
        "  constraints:",
    )
    errors = _validate_yaml(yaml_text)
    assert errors
    assert any("form: human" in e for e in errors)
