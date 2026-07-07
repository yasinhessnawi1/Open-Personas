"""Unit tests for door (b)'s small-tier candidate producer — layer (b) of criterion 8 (Spec A7, T8).

DB-free: a fake backend + a fake grounding source. Proves the producer resolves the FIXED grounding,
scores one candidate (source=EVENT, citation copied from the payload — no fabrication surface), and
degrades every failure (ungroundable / model error / `null` / malformed) to ``None`` (silence, the
A5 fail-soft posture). The wellbeing raise-nothing rule lives in the prompt (defence-in-depth); the
STRUCTURAL exclusion is the handler seam (layer a), proven in the handler + adversarial suites.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.initiative import CandidateSource, CitationKind
from persona.schema.conversation import ConversationMessage
from persona_api.events import SmallTierEventCandidateProducer
from persona_api.events.candidate_handler import EventCandidatePayload
from persona_api.events.candidate_producer import EVENT_CANDIDATE_PROMPT_VERSION

_PAYLOAD = EventCandidatePayload(
    persona_id="pers-1",
    event_kind="connector.message_received",
    event_id="evt-1",
    trigger_id="trg-1",
    human="a message from landlord@x.test arrived",
    causal_chain=("trg-1",),
    grounding_kind="conversation",
    grounding_ref="conv-9",
)
_CTX = SimpleNamespace(owner_id="own-1")

_GOOD_CANDIDATE = """{"candidate": {
  "observation": "The landlord confirmed the inspection is this Friday.",
  "trigger": "approaching_commitment",
  "why_now": "The message just set a dated commitment two days out.",
  "plan": [{"description": "summarise the message", "categories": ["observe"]}],
  "next_step": "Summarise the inspection details for review.",
  "value": 0.8, "acceptance": 0.7, "urgency": "interrupt"}}"""


class _FakeBackend:
    def __init__(self, content: str, *, raises: bool = False) -> None:
        self._content = content
        self._raises = raises
        self.calls = 0

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:  # noqa: ARG002
        self.calls += 1
        if self._raises:
            raise RuntimeError("provider down")
        return ChatResponse(
            content=self._content,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="small",
            provider="local",
            latency_ms=1.0,
        )


class _FakeGrounding:
    def __init__(self, *, conversation: str | None = None, task: str | None = None) -> None:
        self._conversation = conversation
        self._task = task

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None:  # noqa: ARG002
        return self._conversation

    def task_content(self, owner_id: str, task_id: str) -> str | None:  # noqa: ARG002
        return self._task


def _producer(backend: _FakeBackend, grounding: _FakeGrounding) -> SmallTierEventCandidateProducer:
    from persona.initiative import InitiativeSettings

    return SmallTierEventCandidateProducer(
        backend=backend,  # type: ignore[arg-type]
        grounding=grounding,  # type: ignore[arg-type]
        settings=InitiativeSettings(),
    )


@pytest.mark.asyncio
async def test_scores_one_event_candidate_with_fixed_grounding() -> None:
    backend = _FakeBackend(_GOOD_CANDIDATE)
    grounding = _FakeGrounding(conversation="Landlord: the inspection is this Friday at 10am.")
    candidate = await _producer(backend, grounding).produce(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert candidate is not None
    assert candidate.source is CandidateSource.EVENT
    assert candidate.owner_id == "own-1"  # from the context, never the payload
    assert candidate.persona_id == "pers-1"
    assert candidate.prompt_version == EVENT_CANDIDATE_PROMPT_VERSION
    # The citation is FIXED from the payload (A7-D-7) — the model does not choose refs.
    assert len(candidate.citations) == 1
    assert candidate.citations[0].kind is CitationKind.CONVERSATION
    assert candidate.citations[0].ref == "conv-9"


@pytest.mark.asyncio
async def test_ungroundable_event_produces_nothing_without_a_model_call() -> None:
    backend = _FakeBackend(_GOOD_CANDIDATE)
    grounding = _FakeGrounding(conversation=None)  # the conversation vanished
    candidate = await _producer(backend, grounding).produce(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert candidate is None
    assert backend.calls == 0  # no grounding ⇒ no spend (a thin event is success)


@pytest.mark.asyncio
async def test_explicit_null_candidate_is_nothing() -> None:
    backend = _FakeBackend('{"candidate": null}')
    grounding = _FakeGrounding(conversation="A routine newsletter, nothing actionable.")
    candidate = await _producer(backend, grounding).produce(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert candidate is None
    assert backend.calls == 1  # the model was consulted and chose restraint


@pytest.mark.asyncio
async def test_model_failure_degrades_to_silence() -> None:
    backend = _FakeBackend("", raises=True)
    grounding = _FakeGrounding(conversation="some content")
    candidate = await _producer(backend, grounding).produce(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert candidate is None  # a model error is silence, never a crash


@pytest.mark.asyncio
async def test_malformed_candidate_is_dropped_conservatively() -> None:
    # An unknown trigger — the closed catalogue is enforced by construction; drop, don't guess.
    backend = _FakeBackend('{"candidate": {"observation": "x", "trigger": "vibes", "plan": []}}')
    grounding = _FakeGrounding(conversation="some content")
    candidate = await _producer(backend, grounding).produce(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert candidate is None
