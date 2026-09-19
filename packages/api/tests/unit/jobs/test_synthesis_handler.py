"""The A0 synthesis job handler (Spec K2, T8b). Unit-tested with fakes — no DB, no model.

Proves the durable-job glue: marker-CAS idempotency (a re-run past the high-water-mark
does nothing), the windowing handoff (K2-D-5), the meter call (Spec-08 visibility),
and the advance only after a successful synthesise.
"""

# ruff: noqa: ARG002 — fakes ignore some args by design.

from __future__ import annotations

import ast
import contextlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.extraction import ExtractionInput
from persona_api.background import worker_root
from persona_api.jobs.handlers.synthesis import (
    SYNTHESIS_JOB_TYPE,
    InteractionData,
    SynthesisHandler,
    SynthesisJobPayload,
    synthesis_idempotency_key,
)
from persona_api.services.llm_usage_collector import UsageCollectingBackend


class _FakeContext:
    def __init__(self, owner_id: str = "u1") -> None:
        self._owner_id = owner_id
        self.meters: list[dict[str, Any]] = []

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def job_id(self) -> str:
        return "job-1"

    @contextlib.contextmanager
    def connection(self) -> Iterator[object]:
        yield object()

    def meter(
        self, *, amount_micros: int, kind: str, detail: Mapping[str, str] | None = None
    ) -> None:
        self.meters.append({"amount_micros": amount_micros, "kind": kind, "detail": detail})


class _FakeRepo:
    def __init__(self, data: InteractionData | None) -> None:
        self._data = data
        self.advanced: list[int] = []

    def read(
        self, conn: object, *, owner_id: str, payload: SynthesisJobPayload
    ) -> InteractionData | None:
        return self._data

    def advance(
        self, conn: object, *, owner_id: str, payload: SynthesisJobPayload, high_water_mark: int
    ) -> None:
        self.advanced.append(high_water_mark)


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[ExtractionInput] = []

    async def synthesise(self, owner_id: str, interaction: ExtractionInput) -> list[object]:
        self.calls.append(interaction)
        return [object(), object()]  # two merge outcomes


def _payload(hwm: int = 3) -> SynthesisJobPayload:
    return SynthesisJobPayload(
        interaction_kind="conversation",
        interaction_id="conv-1",
        persona_id="p1",
        high_water_mark=hwm,
    )


def _data(up_to: int) -> InteractionData:
    return InteractionData(
        synthesised_up_to=up_to,
        messages=(("user", "I'm a nurse"), ("assistant", "ok"), ("user", "I went vegetarian")),
        compacted_summary="",
    )


def test_idempotency_key_is_kind_interaction_high_water_mark() -> None:
    assert synthesis_idempotency_key(_payload(3)) == "synthesis:conversation:conv-1:3"
    # a continued conversation (new count) re-keys → a new job, not a dedup no-op
    assert synthesis_idempotency_key(_payload(5)) != synthesis_idempotency_key(_payload(3))


def test_job_type_constant() -> None:
    assert SYNTHESIS_JOB_TYPE == "synthesis"


@pytest.mark.asyncio
async def test_happy_path_synthesises_meters_then_advances() -> None:
    ctx, repo, runner = _FakeContext(), _FakeRepo(_data(0)), _FakeRunner()
    await SynthesisHandler(runner=runner, repository=repo).handle(_payload(), ctx)  # type: ignore[arg-type]
    assert len(runner.calls) == 1  # synthesis ran over the windowed tail
    assert "I went vegetarian" in runner.calls[0].content
    assert len(ctx.meters) == 1  # Spec-08 visibility
    assert ctx.meters[0]["kind"] == "model"
    assert repo.advanced == [3]  # marker advanced to the new high-water-mark


@pytest.mark.asyncio
async def test_already_synthesised_is_a_noop_no_synthesise_no_advance() -> None:
    # Marker at the message count → nothing new → idempotent skip (re-run = no dup).
    ctx, repo, runner = _FakeContext(), _FakeRepo(_data(3)), _FakeRunner()
    await SynthesisHandler(runner=runner, repository=repo).handle(_payload(), ctx)  # type: ignore[arg-type]
    assert runner.calls == []
    assert repo.advanced == []
    assert ctx.meters == []


@pytest.mark.asyncio
async def test_missing_interaction_is_a_noop() -> None:
    ctx, repo, runner = _FakeContext(), _FakeRepo(None), _FakeRunner()
    await SynthesisHandler(runner=runner, repository=repo).handle(_payload(), ctx)  # type: ignore[arg-type]
    assert runner.calls == []
    assert repo.advanced == []


# --- the synthesis model call is owner-billed (M3 T5) -------------------------------------


class _BillingBackend:
    provider_name = "openrouter"
    model_name = "m"
    supports_native_tools = False
    supports_vision = False

    async def chat(self, messages: object, **_kw: object) -> ChatResponse:  # noqa: ARG002
        return ChatResponse(
            content="{}",
            usage=TokenUsage(
                prompt_tokens=2000, completion_tokens=300, total_tokens=2300, cost_usd=0.02
            ),
            model="m",
            provider="openrouter",
            latency_ms=1.0,
        )


class _ModelCallingRunner:
    """The extractor as it really behaves: a wrapped backend call inside synthesise."""

    def __init__(self, backend: object) -> None:
        self._backend = backend

    async def synthesise(self, owner_id: str, interaction: object) -> list[object]:  # noqa: ARG002
        await self._backend.chat([])  # records usage into the active sink
        return [object()]


class _RecordingPolicy:
    def __init__(self) -> None:
        self.charges: list[dict[str, Any]] = []

    def capture_up_to_idempotent(self, **kw: Any) -> tuple[int, int]:  # noqa: ANN401
        self.charges.append(kw)
        return int(kw["amount"]), 100


def _billing_handler(policy: _RecordingPolicy | None, repo: _FakeRepo) -> SynthesisHandler:
    return SynthesisHandler(
        runner=_ModelCallingRunner(UsageCollectingBackend(_BillingBackend())),  # type: ignore[arg-type]
        repository=repo,  # type: ignore[arg-type]
        credits_policy=policy,  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_a_synthesis_pass_bills_its_model_call_to_the_owner() -> None:
    """The K2 extractor is a real model call on the owner's behalf with nobody in the loop,
    and it was billed to nobody: the handler never reached bill_background_llm, and its
    backend was composed unmetered so there was no usage to bill from either."""
    policy = _RecordingPolicy()

    await _billing_handler(policy, _FakeRepo(_data(0))).handle(_payload(), _FakeContext())  # type: ignore[arg-type]

    assert len(policy.charges) == 1, "the synthesis model call was not billed"
    charge = policy.charges[0]
    assert charge["cost_cents"] == pytest.approx(2.0)  # 0.02 USD actual
    assert charge["cost_basis"] == "actual_openrouter"
    assert charge["reason"].startswith("synthesis:")


@pytest.mark.asyncio
async def test_the_synthesis_charge_uses_the_jobs_own_key() -> None:
    """A re-delivered synthesis re-runs the extractor and must hit the ON CONFLICT gate."""
    policy = _RecordingPolicy()

    await _billing_handler(policy, _FakeRepo(_data(0))).handle(_payload(), _FakeContext())  # type: ignore[arg-type]
    await _billing_handler(policy, _FakeRepo(_data(0))).handle(_payload(), _FakeContext())  # type: ignore[arg-type]

    keys = {c["billing_key"] for c in policy.charges}
    assert len(keys) == 1, f"a retry would charge under a different key: {keys}"


@pytest.mark.asyncio
async def test_a_no_op_synthesis_charges_nothing() -> None:
    """Nothing past the marker means no model call, so there is nothing to charge for."""
    policy = _RecordingPolicy()

    await _billing_handler(policy, _FakeRepo(_data(99))).handle(_payload(), _FakeContext())  # type: ignore[arg-type]

    assert policy.charges == []


def test_the_worker_root_meters_synthesis_and_passes_a_credits_policy() -> None:
    """The composition half. Both conditions had to hold and neither did, and the two
    comments justified each other: the root said "no owner-billing seam, so wrapping it
    would meter nothing" while the handler said its meter was "a refinement once the
    extractor surfaces per-call usage". Only a check that reads the root can see this."""
    tree = ast.parse(Path(worker_root.__file__).read_text(encoding="utf-8"))

    metered: list[bool] = []
    policies: list[bool] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "plan_scoped_background_backend":
            for kw in node.keywords:
                if kw.arg == "tier" and "synthesis_tier" in ast.dump(kw.value):
                    metered.append(
                        any(
                            k.arg == "metered"
                            and isinstance(k.value, ast.Constant)
                            and k.value.value is True
                            for k in node.keywords
                        )
                    )
        if node.func.id == "register_synthesis_handler":
            policies.append("credits_policy" in {kw.arg for kw in node.keywords})

    assert metered == [True], "the synthesis backend is not metered; its usage cannot be billed"
    assert policies == [True], "the synthesis handler is registered without a credits policy"
