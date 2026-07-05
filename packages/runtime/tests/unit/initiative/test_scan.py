"""Unit tests — the initiative scanner (Spec A5, T5; criteria 1/6/10 scan-side).

Deterministic: fake readers + a stub backend. Pins the T5 bars — thin material
⇒ zero candidates WITHOUT a model call; citations only from actual retrieval
output (invented refs void the candidate); gated-category nodes never reach
the scan prompt (defense-in-depth over the read-side exclusion); the frozen
few-shots (empty-scan + anti-engagement negatives) ride the versioned prompt;
the candidate cap; fail-soft everywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.initiative import (
    CandidateSource,
    InitiativeSettings,
    InitiativeTrigger,
)
from persona_runtime.initiative import (
    INITIATIVE_SCAN_PROMPT_VERSION,
    SCAN_SYSTEM_PROMPT,
    InitiativeScanner,
    ScanConversation,
    ScanNode,
    ScanTask,
)
from persona_runtime.initiative.scan_prompt import (
    EXAMPLE_CATCH_OUTPUT,
    EXAMPLE_EMPTY_SCAN_OUTPUT,
    EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT,
    EXAMPLE_SPECULATION_DECLINE_OUTPUT,
    EXAMPLE_STALE_DECLINE_OUTPUT,
)

_FIRE = datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
_SETTINGS = InitiativeSettings()


class _Readers:
    """One object implementing all three reader Protocols over explicit lists."""

    def __init__(
        self,
        pool: list[ScanNode] | None = None,
        summaries: list[ScanConversation] | None = None,
        tasks: list[ScanTask] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self.pool = pool or []
        self.summaries = summaries or []
        self.tasks = tasks or []
        self.raises = raises

    def noticing_pool(self, owner_id: str, *, limit: int) -> list[ScanNode]:  # noqa: ARG002
        if self.raises:
            msg = "graph unavailable"
            raise RuntimeError(msg)
        return self.pool

    def recent_summaries(self, owner_id: str, *, limit: int) -> list[ScanConversation]:  # noqa: ARG002
        return self.summaries

    def task_history(self, owner_id: str, *, limit: int) -> list[ScanTask]:  # noqa: ARG002
        return self.tasks


class _StubBackend:
    def __init__(self, content: str = EXAMPLE_EMPTY_SCAN_OUTPUT, *, raises: bool = False) -> None:
        self._content = content
        self._raises = raises
        self.calls = 0
        self.last_user_prompt: str | None = None

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        self.last_user_prompt = messages[-1].content
        if self._raises:
            msg = "provider down"
            raise RuntimeError(msg)
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _scanner(readers: _Readers, backend: _StubBackend) -> InitiativeScanner:
    return InitiativeScanner(
        graph=readers, conversations=readers, tasks=readers, backend=backend, settings=_SETTINGS
    )


def _node(node_id: str = "node-91", **overrides: Any) -> ScanNode:  # noqa: ANN401
    fields: dict[str, Any] = {
        "id": node_id,
        "kind": "fact",
        "content": "The custody hearing is on Friday 10 July.",
    }
    fields.update(overrides)
    return ScanNode(**fields)


_CATCH = """{"candidates": [
  {"observation": "The hearing is Friday and nothing is drafted.",
   "trigger": "approaching_commitment",
   "why_now": "The date entered the coming days.",
   "citations": [{"kind": "node", "ref": "node-91"}],
   "plan": [{"description": "draft the letter", "categories": ["draft"]}],
   "next_step": "Draft the response letter.",
   "value": 0.9, "acceptance": 0.8, "urgency": "interrupt"}
]}"""


class TestThinMaterial:
    @pytest.mark.asyncio
    async def test_empty_inputs_yield_silence_without_a_model_call(self) -> None:
        """Criterion 1's structural half: nothing to notice ⇒ no call, no candidates."""
        backend = _StubBackend()
        result = await _scanner(_Readers(), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()
        assert backend.calls == 0

    @pytest.mark.asyncio
    async def test_material_with_empty_model_output_yields_silence(self) -> None:
        backend = _StubBackend(EXAMPLE_EMPTY_SCAN_OUTPUT)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()
        assert backend.calls == 1


class TestCandidateConstruction:
    @pytest.mark.asyncio
    async def test_catch_builds_a_scan_tagged_candidate(self) -> None:
        backend = _StubBackend(_CATCH)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan(
            "user-1", "persona-a", fire_time=_FIRE
        )
        assert len(result) == 1
        candidate = result[0]
        assert candidate.source is CandidateSource.SCAN
        assert candidate.trigger is InitiativeTrigger.APPROACHING_COMMITMENT
        assert candidate.owner_id == "user-1"
        assert candidate.persona_id == "persona-a"
        assert candidate.prompt_version == INITIATIVE_SCAN_PROMPT_VERSION
        assert candidate.scanned_at == _FIRE
        assert candidate.opportunity_key == "approaching_commitment:node/node-91"

    @pytest.mark.asyncio
    async def test_invented_citation_voids_the_candidate(self) -> None:
        """Citations come from ACTUAL retrieval output only — fabrication dies here."""
        invented = _CATCH.replace("node-91", "node-i-made-this-up")
        backend = _StubBackend(invented)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()

    @pytest.mark.asyncio
    async def test_lens_link_targets_are_citable(self) -> None:
        """A node surfaced BY a lens expansion is real retrieval output."""
        from persona_runtime.initiative import ScanLink

        pool = [
            _node(
                links=(
                    ScanLink(
                        link_type="temporal",
                        target_id="node-derived",
                        target_content="The filing precedes the hearing.",
                    ),
                )
            )
        ]
        backend = _StubBackend(_CATCH.replace("node-91", "node-derived"))
        result = await _scanner(_Readers(pool=pool), backend).scan("u", "p", fire_time=_FIRE)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_free_form_trigger_drops_the_candidate(self) -> None:
        loose = _CATCH.replace("approaching_commitment", "interesting_observation")
        backend = _StubBackend(loose)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()

    @pytest.mark.asyncio
    async def test_candidate_cap_truncates_rank_ordered(self) -> None:
        import json

        one = json.loads(_CATCH)["candidates"][0]
        flood = json.dumps({"candidates": [one] * 7})
        backend = _StubBackend(flood)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        # The cap bounds how many are even CONSIDERED (3 by default); identical
        # opportunity keys collapse later at the ledger — here we assert the cap.
        assert len(result) <= _SETTINGS.scan_candidate_cap


class TestSubjectExclusion:
    @pytest.mark.asyncio
    async def test_wellbeing_tagged_node_never_reaches_the_prompt(self) -> None:
        """Criterion 6, scan side: even a misbehaving reader cannot put tagged
        content in front of the scan model (defense-in-depth over the read)."""
        tagged = _node(
            "node-sensitive",
            content="disclosed self-harm struggles last month",
            wellbeing_category="self_harm",
        )
        backend = _StubBackend()
        await _scanner(_Readers(pool=[tagged, _node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert backend.last_user_prompt is not None
        assert "node-sensitive" not in backend.last_user_prompt
        assert "self-harm" not in backend.last_user_prompt

    @pytest.mark.asyncio
    async def test_self_node_never_reaches_the_prompt(self) -> None:
        backend = _StubBackend()
        await _scanner(_Readers(pool=[_node("node-self", kind="self"), _node()]), backend).scan(
            "u", "p", fire_time=_FIRE
        )
        assert backend.last_user_prompt is not None
        assert "node-self" not in backend.last_user_prompt


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_reader_error_yields_silence(self) -> None:
        backend = _StubBackend(_CATCH)
        result = await _scanner(_Readers(raises=True), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()
        assert backend.calls == 0

    @pytest.mark.asyncio
    async def test_backend_error_yields_silence(self) -> None:
        backend = _StubBackend(raises=True)
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()

    @pytest.mark.asyncio
    async def test_garbage_output_yields_silence(self) -> None:
        backend = _StubBackend("Here are my thoughts on what you should tell the user...")
        result = await _scanner(_Readers(pool=[_node()]), backend).scan("u", "p", fire_time=_FIRE)
        assert result == ()


class TestPromptArtifact:
    def test_version_constant_and_frozen_few_shots(self) -> None:
        assert INITIATIVE_SCAN_PROMPT_VERSION == "a5-scan-v1"
        # The empty-scan example and every anti-engagement/stale/speculation
        # negative is an EMPTY output — the frozen spec-by-example of restraint.
        assert EXAMPLE_EMPTY_SCAN_OUTPUT == '{"candidates": []}'
        assert EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT == '{"candidates": []}'
        assert EXAMPLE_STALE_DECLINE_OUTPUT == '{"candidates": []}'
        assert EXAMPLE_SPECULATION_DECLINE_OUTPUT == '{"candidates": []}'
        for example in (
            EXAMPLE_EMPTY_SCAN_OUTPUT,
            EXAMPLE_CATCH_OUTPUT,
        ):
            assert example in SCAN_SYSTEM_PROMPT

    def test_prompt_carries_the_anti_engagement_rule(self) -> None:
        assert "check-ins" in SCAN_SYSTEM_PROMPT or "check-in" in SCAN_SYSTEM_PROMPT
        assert "conversation-starters" in SCAN_SYSTEM_PROMPT

    def test_prompt_carries_the_closed_catalogue(self) -> None:
        for trigger in InitiativeTrigger:
            assert trigger.value in SCAN_SYSTEM_PROMPT
