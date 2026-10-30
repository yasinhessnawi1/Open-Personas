"""Initiative's model calls run on ``PERSONA_INITIATIVE_MODEL_TIER`` (owner ruling 2026-10-30).

The scan, the grounding judge and A7's producer share ONE backend, built by
:func:`initiative_model_backend`: plan-scoped (a free owner reaches the FREE registry's tier
of the same name, never a paid model), metered, and routed on the configured tier. The
judge's head-of-chain identity is read from the same resolution, so it follows the tier.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import BackendConfig
from persona.backends.types import ChatResponse, TokenUsage
from persona.initiative import InitiativeSettings
from persona.schema.conversation import ConversationMessage
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import subscription as subscription_t
from persona_api.initiative.pipeline_wiring import initiative_model_backend
from persona_api.middleware.rls_context import current_user_id
from persona_runtime.initiative.grounding import head_identity
from persona_runtime.tier import TierConfig, TierRegistry
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_alice"


class _Model:
    """A chain stand-in that records its calls ("which model ran" is observable)."""

    provider_name = "openrouter"
    supports_native_tools = False
    supports_vision = False

    def __init__(self, model: str) -> None:
        self.model_name = model
        self.calls = 0

    async def chat(self, messages: Any, **_: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        return ChatResponse(
            content="ok",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=self.model_name,
            provider="openrouter",
            latency_ms=1.0,
        )


def _registry(**tiers: _Model) -> TierRegistry:
    return TierRegistry(
        {
            name: TierConfig(
                name=name,
                backend_config=BackendConfig(provider="anthropic", model=m.model_name, api_key="k"),
                preconstructed_backend=m,  # type: ignore[arg-type]
            )
            for name, m in tiers.items()
        }
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_community_engine(tmp_path / "plans.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="alice@example.com")
    return eng


@contextlib.contextmanager
def _owner(owner_id: str) -> Iterator[None]:
    token = current_user_id.set(owner_id)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _set_plan(engine: Engine, plan_code: str) -> None:
    with engine.begin() as conn:
        conn.execute(insert(subscription_t).values(user_id=_OWNER, plan_code=plan_code))


_MESSAGES = [ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))]


def _chains() -> tuple[dict[str, _Model], dict[str, _Model]]:
    paid = {t: _Model(f"paid-{t}") for t in ("small", "mid", "frontier")}
    free = {t: _Model(f"free-{t}:free") for t in ("small", "mid", "frontier")}
    return paid, free


@pytest.mark.parametrize(("plan", "side"), [("plus", "paid"), ("free", "free")])
@pytest.mark.asyncio
async def test_the_default_mid_tier_runs_on_the_owners_plan(
    engine: Engine, plan: str, side: str
) -> None:
    _set_plan(engine, plan)
    paid, free = _chains()
    backend = initiative_model_backend(
        InitiativeSettings(),
        rls_engine=engine,
        paid_tier_registry=_registry(**paid),
        free_tier_registry=_registry(**free),
    )
    with _owner(_OWNER):
        await backend.chat(_MESSAGES)
        head = head_identity(backend)
    chosen = paid if side == "paid" else free
    other = free if side == "paid" else paid
    assert chosen["mid"].calls == 1
    assert sum(m.calls for m in other.values()) == 0  # a free owner never reaches paid
    assert chosen["small"].calls == chosen["frontier"].calls == 0
    assert head == f"openrouter/{chosen['mid'].model_name}"  # the judge's head follows
    from persona_api.services.llm_usage_collector import UsageCollectingBackend

    assert isinstance(backend, UsageCollectingBackend)  # metered: the owner's cost is recorded


@pytest.mark.parametrize("tier", ["small", "frontier"])
@pytest.mark.asyncio
async def test_the_configured_tier_is_the_one_called(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    _set_plan(engine, "plus")
    monkeypatch.setenv("PERSONA_INITIATIVE_MODEL_TIER", tier)
    paid, free = _chains()
    backend = initiative_model_backend(
        InitiativeSettings(),
        rls_engine=engine,
        paid_tier_registry=_registry(**paid),
        free_tier_registry=_registry(**free),
    )
    with _owner(_OWNER):
        await backend.chat(_MESSAGES)
    assert [t for t, m in paid.items() if m.calls] == [tier]


def test_the_dial_verbs_run_on_the_same_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """The confirm/decline verb interpreter follows the initiative tier too."""
    from persona_api.services.runtime_factory import RuntimeFactory

    asked: list[str] = []

    class _Plan:
        def get(self, tier: str) -> _Model:
            asked.append(tier)
            return _Model(f"paid-{tier}")

    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    monkeypatch.setenv("PERSONA_INITIATIVE_VERB_MAX_OUTPUT_TOKENS", "777")
    monkeypatch.setenv("PERSONA_INITIATIVE_VERB_TIMEOUT_SECONDS", "2.5")
    factory = RuntimeFactory.__new__(RuntimeFactory)
    monkeypatch.setattr(factory, "_plan_tier_registry", _Plan, raising=False)
    interpreter = factory._build_initiative_verb_interpreter()  # noqa: SLF001
    assert interpreter is not None
    assert asked == ["mid"]
    assert interpreter._max_output_tokens == 777  # noqa: SLF001  # the setting, wired
    assert interpreter._timeout_seconds == 2.5  # noqa: SLF001


def test_the_worker_gives_the_judge_its_context() -> None:
    """F2 at the composition root: the grounding judge's source carries the graph store and
    the scan's wellbeing gate (without them the judge would see no CONTEXT)."""
    import ast
    import inspect

    from persona_api.background import worker_root

    tree = ast.parse(inspect.getsource(worker_root))
    sources = [
        kw.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "GroundingChecker"
        for kw in node.keywords
        if kw.arg == "source"
    ]
    assert len(sources) == 1
    call = sources[0]
    assert isinstance(call, ast.Call)
    wired = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    assert wired == {"gate": "scan_gate"}


def test_the_renamed_tier_variable_is_named_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review LOW-8: PERSONA_INITIATIVE_SCAN_TIER is ignored since the rename; a deployment
    still setting it is told so at startup (and the worker root asks)."""
    import ast
    import inspect

    from loguru import logger
    from persona.initiative.config import warn_renamed_initiative_settings
    from persona_api.background import worker_root

    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(str(m.record["message"])), level="WARNING")
    try:
        monkeypatch.delenv("PERSONA_INITIATIVE_SCAN_TIER", raising=False)
        assert warn_renamed_initiative_settings() == []
        assert seen == []
        monkeypatch.setenv("PERSONA_INITIATIVE_SCAN_TIER", "small")
        assert warn_renamed_initiative_settings() == ["PERSONA_INITIATIVE_SCAN_TIER"]
    finally:
        logger.remove(sink)
    assert any(
        "PERSONA_INITIATIVE_SCAN_TIER" in m and "PERSONA_INITIATIVE_MODEL_TIER" in m for m in seen
    )
    tree = ast.parse(inspect.getsource(worker_root.build_worker_registry))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "warn_renamed_initiative_settings"
    ]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_free_owner_without_a_free_mid_chain_never_reaches_paid(
    engine: Engine,
) -> None:
    """Review LOW-8: FREE_MID unset. The free registry falls back to a FREE tier it has
    (logged), never to a paid one; with no free tier at all the call raises the documented
    not-configured error, and the judge reads that as a judge error: fail-soft, nothing
    raised, the paid chain never touched."""
    from persona.backends.errors import TierNotConfiguredError as BackendTierNotConfiguredError
    from persona_runtime.errors import TierNotConfiguredError as RegistryTierNotConfiguredError
    from persona_runtime.initiative.grounding import GroundingChecker, GroundingRejection

    _set_plan(engine, "free")
    paid, _free = _chains()
    free_small = _Model("free-small:free")
    fallback = initiative_model_backend(
        InitiativeSettings(),
        rls_engine=engine,
        paid_tier_registry=_registry(**paid),
        free_tier_registry=_registry(small=free_small),
    )
    with _owner(_OWNER):
        await fallback.chat(_MESSAGES)
    assert free_small.calls == 1
    assert sum(m.calls for m in paid.values()) == 0
    backend = initiative_model_backend(
        InitiativeSettings(),
        rls_engine=engine,
        paid_tier_registry=_registry(**paid),
        free_tier_registry=TierRegistry({}),
    )
    with (
        _owner(_OWNER),
        pytest.raises((BackendTierNotConfiguredError, RegistryTierNotConfiguredError)),
    ):
        await backend.chat(_MESSAGES)
    assert sum(m.calls for m in paid.values()) == 0

    class _Source:
        def node_content(self, owner_id: str, node_id: str) -> str | None:  # noqa: ARG002
            return "The user's car insurance renews automatically on 4 October 2026."

        def conversation_content(self, owner_id: str, ref: str) -> str | None:  # noqa: ARG002
            return None

        def task_content(self, owner_id: str, ref: str) -> str | None:  # noqa: ARG002
            return None

        def node_context(self, owner_id: str, node_id: str, *, limit: int) -> list[Any]:  # noqa: ARG002
            return []

    from persona.initiative import (
        CandidateSource,
        CitationKind,
        GroundingCitation,
        InitiativeCandidate,
        InitiativeTrigger,
        PlannedStep,
        Urgency,
    )
    from persona.tools.categories import ActionCategory

    candidate = InitiativeCandidate(
        observation="The car insurance renews automatically.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref="n1"),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="Your car insurance renews automatically.",
        plan=(PlannedStep(description="compare", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="compare offers for you",
        value=0.8,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id="p1",
        prompt_version="a5-scan-v9",
        scanned_at=datetime(2026, 9, 26, 5, 0, tzinfo=UTC),
    )
    with _owner(_OWNER):
        verdict = await GroundingChecker(source=_Source(), backend=backend).check(candidate)
    assert verdict.rejection is GroundingRejection.JUDGE_ERROR
    assert sum(m.calls for m in paid.values()) == 0


@pytest.mark.parametrize("provenance", [None, {"interaction_id": "c1"}, "c1", 7])
def test_an_unreadable_provenance_raises_instead_of_skipping_the_gate(provenance: object) -> None:
    """Re-review LOW-2: a provenance that is not a list must not read as "no sources" (which
    would skip the interaction gate); it raises, and the judge's context read becomes
    CONTEXT_UNAVAILABLE."""
    from persona_api.initiative.readers import _row_sources

    with pytest.raises(ValueError, match="provenance"):
        _row_sources(provenance)
    assert _row_sources([{"interaction_id": "c1"}, {"interaction_id": None}, {}]) == {"c1"}


@pytest.mark.parametrize("entry", ["c1", None, 7, ["c1"]])
def test_an_unreadable_provenance_entry_raises_too(entry: object) -> None:
    """Third review LOW-3: one entry that is not an object must not be skipped silently."""
    from persona_api.initiative.readers import _row_sources

    with pytest.raises(ValueError, match="provenance"):
        _row_sources([{"interaction_id": "c1"}, entry])
