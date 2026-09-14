"""The acceptance assessor proposes; core decides (R9-164).

What matters here is mostly the silence. No backend, a timeout, a raise, prose instead of
JSON, a claim with an unusable status: every one of them yields no claims, and criteria that
do not advance simply stay pending, which is what the system did before this existed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.tools import PersistedArtifact, ToolCall, ToolResult
from persona.tasks import (
    AcceptanceCriterion,
    AcceptanceStatus,
    Contract,
    settle_criteria,
)
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs.acceptance import AcceptanceAssessor, evidence_from_run

if TYPE_CHECKING:
    from persona.schema.conversation import ConversationMessage

_NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
_CONTRACT = Contract(
    goal="compare rental deposit schemes",
    acceptance_criteria=(
        AcceptanceCriterion(id="c1", statement="three schemes compared"),
        AcceptanceCriterion(id="c2", statement="written to a file"),
    ),
)


class _ScriptedBackend:
    def __init__(
        self, content: str = "", *, raises: BaseException | None = None, delay_s: float = 0.0
    ) -> None:
        self._content = content
        self._raises = raises
        self._delay_s = delay_s
        self.prompts: list[str] = []

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_kwargs: Any) -> ChatResponse:  # noqa: ANN401
        self.prompts.append("\n".join(str(m.content) for m in messages))
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._raises is not None:
            raise self._raises
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _run(*, wrote: str | None = None, status: RunStatus = RunStatus.COMPLETED) -> Run:
    steps: list[Step] = [
        Step(
            type=StepType.TOOL_CALL,
            tool_calls=[
                ToolCall(name="web_search", args={"query": "deposit schemes"}, call_id="a")
            ],
            results=[
                ToolResult(
                    tool_name="web_search",
                    call_id="a",
                    content="three results",
                    data={"results": [{"url": "https://lovdata.no/husleieloven"}]},
                )
            ],
        )
    ]
    if wrote is not None:
        steps.append(
            Step(
                type=StepType.TOOL_CALL,
                tool_calls=[ToolCall(name="file_write", args={"path": wrote}, call_id="b")],
                results=[
                    ToolResult(
                        tool_name="file_write",
                        call_id="b",
                        content=f"wrote {wrote}",
                        artifacts=(
                            PersistedArtifact(
                                workspace_path=wrote, mime_type="text/markdown", size_bytes=10
                            ),
                        ),
                    )
                ],
            )
        )
    return Run(
        persona_id="p",
        task="x",
        status=status,
        steps=steps,
        output="compared them",
        started_at=_NOW,
        finished_at=_NOW,
    )


def _claims(*items: dict[str, str]) -> str:
    return json.dumps({"claims": list(items)})


def _assessor(backend: _ScriptedBackend | None, *, timeout_s: float = 20.0) -> AcceptanceAssessor:
    return AcceptanceAssessor(
        backend_provider=lambda: backend,  # type: ignore[arg-type, return-value]
        timeout_s=timeout_s,
    )


# --- evidence ----------------------------------------------------------------


def test_evidence_is_this_legs_own_files_and_sources() -> None:
    evidence = evidence_from_run(_run(wrote="reports/deposits.md"))
    assert evidence.artifacts == ("reports/deposits.md",)
    assert evidence.sources == ("https://lovdata.no/husleieloven",)
    assert evidence.errored is False


def test_an_errored_run_is_marked_as_such() -> None:
    assert evidence_from_run(_run(status=RunStatus.ERROR)).errored is True


# --- proposing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_is_proposed_and_the_gate_accepts_it() -> None:
    backend = _ScriptedBackend(
        _claims({"id": "c2", "status": "done", "evidence": "wrote reports/deposits.md"})
    )
    run = _run(wrote="reports/deposits.md")

    claims = await _assessor(backend).assess(contract=_CONTRACT, run=run)
    criteria, rejected = settle_criteria(_CONTRACT, claims, evidence_from_run(run))

    assert rejected == ()
    assert criteria[1].status is AcceptanceStatus.DONE
    # The prompt shows the model exactly what it may cite, and the open criteria only.
    assert "file written: reports/deposits.md" in backend.prompts[0]
    assert "c2" in backend.prompts[0]


@pytest.mark.asyncio
async def test_a_fabricated_citation_proposes_but_never_lands() -> None:
    """The assessor is allowed to be wrong; the gate is what stops it."""
    backend = _ScriptedBackend(
        _claims({"id": "c1", "status": "done", "evidence": "wrote reports/never-written.md"})
    )
    run = _run(wrote="reports/deposits.md")

    claims = await _assessor(backend).assess(contract=_CONTRACT, run=run)
    criteria, rejected = settle_criteria(_CONTRACT, claims, evidence_from_run(run))

    assert len(claims) == 1  # it proposed
    assert criteria == _CONTRACT.acceptance_criteria  # and nothing moved
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_a_settled_contract_is_never_put_to_a_model() -> None:
    backend = _ScriptedBackend(_claims({"id": "c1", "status": "done", "evidence": "x"}))
    all_done = _CONTRACT.model_copy(
        update={
            "acceptance_criteria": tuple(
                c.model_copy(update={"status": AcceptanceStatus.DONE})
                for c in _CONTRACT.acceptance_criteria
            )
        }
    )
    assert await _assessor(backend).assess(contract=all_done, run=_run()) == ()
    assert backend.prompts == []  # no criteria open, no call, no cost


@pytest.mark.asyncio
async def test_an_errored_leg_is_never_put_to_a_model() -> None:
    """The gate would refuse every claim from it, so paying for the call buys nothing."""
    backend = _ScriptedBackend(_claims({"id": "c1", "status": "done", "evidence": "x"}))
    assert (
        await _assessor(backend).assess(contract=_CONTRACT, run=_run(status=RunStatus.ERROR)) == ()
    )
    assert backend.prompts == []


# --- silence is the failure mode ---------------------------------------------


@pytest.mark.asyncio
async def test_no_backend_claims_nothing() -> None:
    assert await _assessor(None).assess(contract=_CONTRACT, run=_run()) == ()


@pytest.mark.asyncio
async def test_a_timeout_claims_nothing() -> None:
    backend = _ScriptedBackend(
        _claims({"id": "c1", "status": "done", "evidence": "x"}), delay_s=0.2
    )
    assert await _assessor(backend, timeout_s=0.01).assess(contract=_CONTRACT, run=_run()) == ()


@pytest.mark.asyncio
async def test_a_raising_backend_claims_nothing() -> None:
    backend = _ScriptedBackend(raises=RuntimeError("provider down"))
    assert await _assessor(backend).assess(contract=_CONTRACT, run=_run()) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "I think criterion one is done!",
        "",
        "{}",
        '{"claims": "c1"}',
        '{"claims": [{"id": "", "status": "done"}]}',
        '{"claims": [{"id": "c1", "status": "pending"}]}',
        '{"claims": [{"id": "c1", "status": "probably"}]}',
        '{"claims": ["c1"]}',
    ],
)
async def test_unusable_output_claims_nothing(content: str) -> None:
    assert await _assessor(_ScriptedBackend(content)).assess(contract=_CONTRACT, run=_run()) == ()


@pytest.mark.asyncio
async def test_a_fenced_answer_still_parses() -> None:
    fenced = (
        "```json\n" + _claims({"id": "c1", "status": "failed", "evidence": "only two"}) + "\n```"
    )
    claims = await _assessor(_ScriptedBackend(fenced)).assess(contract=_CONTRACT, run=_run())
    assert [(c.criterion_id, c.status) for c in claims] == [("c1", AcceptanceStatus.FAILED)]
