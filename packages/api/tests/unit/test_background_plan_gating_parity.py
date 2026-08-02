"""A background job runs on the JOB OWNER's plan models, exactly like their chat (R9-096).

The defect: every background LLM backend in the in-process worker was resolved ONCE, at
worker startup, from the app's PAID :class:`~persona_runtime.tier.TierRegistry` —

    episodic_summarizer = TierSummarizer(
        backend=UsageCollectingBackend(tier_registry.get(config.episodic_summary_tier))
    )

— with no owner bound. One instance cannot resolve per owner, so EVERY owner's episodic
consolidation, initiative scan, title refresh, synthesis and file-extract ran on paid
models. In production a free-plan account with 2 personas was charged 247 credits across
3 episodic consolidations at ``cost_basis='estimate_static'`` (paid-model static pricing),
then doubled by the credit markup. That is both a real cost leak and a D-M4-9 violation:
*a free user must NEVER reach a paid model.*

This is the exact sibling of R9-074 (the connector composing from the global registry
once), so the tests here are the R9-074 shape:

* the gate is proven BEHAVIOURALLY — real ``subscription`` rows, the real plan read, and
  an assertion about which model actually received the call, never "a function was called";
* the pre-fix composition is exercised side by side as a COUNTERFACTUAL, so the fix stays
  falsifiable — revert it and :func:`test_the_pre_fix_worker_shape_sent_a_free_owner_to_a_
  paid_model` is the only one that keeps passing;
* community stays ungated, and is asserted to not even READ the plan (the engine it is
  handed explodes on use).

The end-to-end half runs the REAL ``EpisodicConsolidationHandler`` over the REAL
``TierSummarizer`` composed the way the worker composes it, so it also pins the thing that
would be easy to lose while fixing this: the ``UsageCollectingBackend`` wrapper M3 bills
from. A fix that gated correctly but stopped metering would pass a narrower test.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import BackendConfig
from persona.backends.errors import TierNotConfiguredError as BackendTierNotConfiguredError
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.conversation import ConversationMessage
from persona.stores.summarizer import TierSummarizer
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import subscription as subscription_t
from persona_api.jobs.handlers.episodic_consolidation import (
    EpisodicConsolidationHandler,
    EpisodicConsolidationJobPayload,
)
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.llm_usage_collector import UsageCollectingBackend
from persona_api.services.model_tiers import (
    plan_scoped_background_backend,
    select_plan_tier_registry,
)
from persona_runtime.errors import TierNotConfiguredError as RegistryTierNotConfiguredError
from persona_runtime.tier import TierConfig, TierRegistry
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_alice"
_TIER = "mid"

#: Both flavours of "this plan has no such tier" — sibling classes, not aliases.
_UNCONFIGURED = (BackendTierNotConfiguredError, RegistryTierNotConfiguredError)


class _RecordingBackend:
    """A ChatBackend that records every call, so "which model ran" is observable."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "openrouter"

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **_kw: object) -> ChatResponse:  # noqa: ARG002
        self.calls += 1
        return ChatResponse(
            content="a summary",
            usage=TokenUsage(
                prompt_tokens=100, completion_tokens=1000, total_tokens=1100, cost_usd=0.03
            ),
            model=self.model,
            provider="openrouter",
            latency_ms=1.0,
        )


class _ExplodingEngine:
    """Stands in for the RLS engine where NO plan read may happen."""

    def begin(self) -> Any:  # noqa: ANN401 — never reached; the call itself is the assertion
        raise AssertionError("the plan must not be read when gating is off (community)")


def _registry(backend: _RecordingBackend) -> TierRegistry:
    """A registry whose ``get`` returns ``backend`` — the seeded preconstructed path."""
    return TierRegistry(
        {
            _TIER: TierConfig(
                name=_TIER,
                backend_config=BackendConfig(
                    provider="anthropic", model=backend.model, api_key="k"
                ),
                preconstructed_backend=backend,  # type: ignore[arg-type]
            )
        }
    )


@contextlib.contextmanager
def _owner_scope(owner_id: str) -> Iterator[None]:
    """Bind the owner the way the worker's per-job choke point does."""
    token = current_user_id.set(owner_id)
    try:
        yield
    finally:
        current_user_id.reset(token)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A real (SQLite) engine with the real ``subscription`` table."""
    eng = make_community_engine(tmp_path / "plans.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="alice@example.com")
    return eng


def _set_plan(eng: Engine, plan_code: str) -> None:
    with eng.begin() as conn:
        conn.execute(insert(subscription_t).values(user_id=_OWNER, plan_code=plan_code))


async def _run(backend: object) -> None:
    """One background model call through whatever backend the composition produced."""
    from datetime import UTC, datetime

    message = ConversationMessage(role="user", content="summarise", created_at=datetime.now(UTC))
    await backend.chat([message])  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The gate, behaviourally: which model does a background job actually reach?
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_free_owners_background_job_reaches_the_free_model(engine: Engine) -> None:
    """THE assertion: a free-plan owner's background call lands on the FREE model.

    Real ``subscription`` row, real plan read, and the paid backend is asserted untouched —
    a free user must never reach a paid model, not even by fallback.
    """
    _set_plan(engine, "free")
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")
    backend = plan_scoped_background_backend(
        tier=_TIER,
        rls_engine=engine,
        paid_tier_registry=_registry(paid),
        free_tier_registry=_registry(free),
        metered=True,
    )

    with _owner_scope(_OWNER):
        await _run(backend)

    assert free.calls == 1
    assert paid.calls == 0


@pytest.mark.asyncio
async def test_a_paid_owners_background_job_still_reaches_the_paid_model(engine: Engine) -> None:
    """Gating must not over-reach: a paying customer's background work keeps the paid tiers."""
    _set_plan(engine, "pro")
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")
    backend = plan_scoped_background_backend(
        tier=_TIER,
        rls_engine=engine,
        paid_tier_registry=_registry(paid),
        free_tier_registry=_registry(free),
        metered=True,
    )

    with _owner_scope(_OWNER):
        await _run(backend)

    assert paid.calls == 1
    assert free.calls == 0


@pytest.mark.asyncio
async def test_one_backend_instance_serves_two_owners_on_their_own_plans(engine: Engine) -> None:
    """The structural crux: the SAME long-lived instance follows whoever is running.

    This is what the startup-resolved backend could not do, and why the worker leaked. The
    worker composes one backend per surface and runs every owner's jobs through it, so the
    per-owner behaviour has to hold for a shared instance, not just a fresh one.
    """
    _set_plan(engine, "free")
    ensure_owner(engine, owner_id="user_bob", email="bob@example.com")
    with engine.begin() as conn:
        conn.execute(insert(subscription_t).values(user_id="user_bob", plan_code="plus"))
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")
    backend = plan_scoped_background_backend(
        tier=_TIER,
        rls_engine=engine,
        paid_tier_registry=_registry(paid),
        free_tier_registry=_registry(free),
        metered=True,
    )

    with _owner_scope(_OWNER):
        await _run(backend)
    with _owner_scope("user_bob"):
        await _run(backend)

    assert (free.calls, paid.calls) == (1, 1)


@pytest.mark.asyncio
async def test_no_owner_bound_falls_back_to_free_never_paid(engine: Engine) -> None:
    """Fail-safe: off-job (no owner in scope) resolves the RESTRICTIVE set.

    Worker composition itself happens with no owner bound; if that resolved the paid
    registry the gate would be decorative.
    """
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")
    backend = plan_scoped_background_backend(
        tier=_TIER,
        rls_engine=engine,
        paid_tier_registry=_registry(paid),
        free_tier_registry=_registry(free),
        metered=True,
    )

    await _run(backend)

    assert (free.calls, paid.calls) == (1, 0)


@pytest.mark.asyncio
async def test_an_empty_free_registry_fails_closed_instead_of_falling_back(
    engine: Engine,
) -> None:
    """Fail-closed (M4): nothing free configured ⇒ raise, never a quiet paid fallback.

    The documented posture: ``TierNotConfiguredError`` reaches the surface's own fail-soft
    catch, which treats it as "not wired" and skips the work. Skipping is the correct
    outcome; billing a free user for a paid model is not.
    """
    _set_plan(engine, "free")
    paid = _RecordingBackend("paid-model")
    backend = plan_scoped_background_backend(
        tier=_TIER,
        rls_engine=engine,
        paid_tier_registry=_registry(paid),
        free_tier_registry=TierRegistry({}),  # cloud, nothing free configured
        metered=True,
    )

    with _owner_scope(_OWNER), pytest.raises(_UNCONFIGURED):
        await _run(backend)

    assert paid.calls == 0


def test_metadata_reads_stay_quiet_when_the_plan_has_no_tier(engine: Engine) -> None:
    """A metadata read must degrade, not raise — callers do it from inside ``except``.

    ``TierSummarizer`` reports ``provider_name`` while handling a failed call; a raising
    property there would replace the honest domain error with a confusing one.
    """
    _set_plan(engine, "free")
    backend = plan_scoped_background_backend(
        tier=_TIER,
        rls_engine=engine,
        paid_tier_registry=_registry(_RecordingBackend("paid-model")),
        free_tier_registry=TierRegistry({}),
        metered=False,
    )

    with _owner_scope(_OWNER):
        assert backend.provider_name == "unresolved"
        assert backend.model_name == "unresolved"
        assert backend.supports_vision is False
        assert backend.supports_native_tools is False


@pytest.mark.asyncio
async def test_community_is_ungated_and_never_even_reads_a_plan() -> None:
    """Community (no free registry) must be byte-identical: paid tiers, zero plan reads.

    The engine handed in raises on any use, so a plan read would fail the test rather than
    silently add a query to every self-hosted background job.
    """
    paid = _RecordingBackend("paid-model")
    backend = plan_scoped_background_backend(
        tier=_TIER,
        rls_engine=_ExplodingEngine(),  # type: ignore[arg-type]
        paid_tier_registry=_registry(paid),
        free_tier_registry=None,  # community / self-host: no plans, nothing to gate
        metered=False,
    )

    with _owner_scope(_OWNER):
        await _run(backend)

    assert paid.calls == 1


def test_the_shared_selector_is_what_both_halves_of_the_gate_use(engine: Engine) -> None:
    """``select_plan_tier_registry`` is the ONE plan decision (chat + background).

    R9-074's lesson was that a decision retyped at each composition root drifts at one of
    them. The chat path (``RuntimeFactory._plan_tier_selection``) and every background
    surface now resolve through this function, so there is a single thing to get right.
    """
    _set_plan(engine, "free")
    paid, free = _registry(_RecordingBackend("paid")), _registry(_RecordingBackend("free"))

    with _owner_scope(_OWNER):
        chosen = select_plan_tier_registry(
            rls_engine=engine, paid_tier_registry=paid, free_tier_registry=free
        )
    assert chosen is free

    with _owner_scope(_OWNER):
        ungated = select_plan_tier_registry(
            rls_engine=engine, paid_tier_registry=paid, free_tier_registry=None
        )
    assert ungated is paid


# ---------------------------------------------------------------------------
# The counterfactual — the fix has to stay falsifiable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pre_fix_worker_shape_sent_a_free_owner_to_a_paid_model(engine: Engine) -> None:
    """The shipped composition, reproduced verbatim, billing a free owner on paid models.

    ``UsageCollectingBackend(tier_registry.get(tier))`` — resolved once, at startup, off
    the paid registry — is what ``worker_root`` used to build. If that shape ever comes
    back, the tests above start failing while this one keeps passing, which is exactly the
    signal we want.
    """
    _set_plan(engine, "free")
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")
    pre_fix = UsageCollectingBackend(_registry(paid).get(_TIER))  # the old composition

    with _owner_scope(_OWNER):
        await _run(pre_fix)

    assert paid.calls == 1  # a free-plan owner, on the paid model
    assert free.calls == 0


# ---------------------------------------------------------------------------
# End to end through the REAL episodic handler (and its billing)
# ---------------------------------------------------------------------------


class _RecordingPolicy:
    """Captures what the handler billed (the ``test_episodic_billing`` shape)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        return int(kw["amount"]), 0  # type: ignore[call-overload]


class _SummarizingEngine:
    """A consolidation engine that summarises once, like the K8 engine's window pass."""

    def __init__(self, summarizer: TierSummarizer) -> None:
        self._summarizer = summarizer

    async def run(self, owner_id: str, persona_id: str) -> object:  # noqa: ARG002
        await self._summarizer.summarize("some raw episodes", target_tokens=64)
        return SimpleNamespace(windows_formed=1, gists_written=1, candidates_emitted=0, skipped=[])


def _ctx(owner_id: str) -> object:
    return SimpleNamespace(owner_id=owner_id, job_id="j1", meter=lambda **_k: None)


@pytest.mark.asyncio
async def test_a_free_owners_episodic_consolidation_summarises_on_the_free_model_and_still_bills(
    engine: Engine,
) -> None:
    """The reported defect, end to end — and metering survives the fix.

    Composes the summarizer the way ``worker_root`` composes it, then runs the REAL
    ``EpisodicConsolidationHandler``. Two assertions, both load-bearing: the free model did
    the work (the leak is closed) AND the owner was still billed from real captured usage
    (the ``UsageCollectingBackend`` wrapper M3 needs is still in the chain — losing it
    would stop metering silently).
    """
    _set_plan(engine, "free")
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")
    summarizer = TierSummarizer(
        backend=plan_scoped_background_backend(
            tier=_TIER,
            rls_engine=engine,
            paid_tier_registry=_registry(paid),
            free_tier_registry=_registry(free),
            metered=True,
        )
    )
    policy = _RecordingPolicy()
    handler = EpisodicConsolidationHandler(
        engine=_SummarizingEngine(summarizer),  # type: ignore[arg-type]
        credits_policy=policy,  # type: ignore[arg-type]
        rls_engine=engine,
        cost_source=None,
    )

    with _owner_scope(_OWNER):
        await handler.handle(
            EpisodicConsolidationJobPayload(persona_id="p1", watermark_bucket="b1"),
            _ctx(_OWNER),  # type: ignore[arg-type]
        )

    assert (free.calls, paid.calls) == (1, 0)  # the leak, closed
    assert len(policy.calls) == 1  # still metered…
    assert policy.calls[0]["amount"] == 3  # …from the REAL captured usage (ceil 3.0¢)
    assert policy.calls[0]["cost_basis"] == "actual_openrouter"


@pytest.mark.asyncio
async def test_a_paid_owners_episodic_consolidation_is_unchanged(engine: Engine) -> None:
    """The other half of parity: a paying owner's consolidation still runs on paid models."""
    _set_plan(engine, "plus")
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")
    summarizer = TierSummarizer(
        backend=plan_scoped_background_backend(
            tier=_TIER,
            rls_engine=engine,
            paid_tier_registry=_registry(paid),
            free_tier_registry=_registry(free),
            metered=True,
        )
    )
    handler = EpisodicConsolidationHandler(
        engine=_SummarizingEngine(summarizer),  # type: ignore[arg-type]
        credits_policy=_RecordingPolicy(),  # type: ignore[arg-type]
        rls_engine=engine,
        cost_source=None,
    )

    with _owner_scope(_OWNER):
        await handler.handle(
            EpisodicConsolidationJobPayload(persona_id="p1", watermark_bucket="b1"),
            _ctx(_OWNER),  # type: ignore[arg-type]
        )

    assert (paid.calls, free.calls) == (1, 0)


# ---------------------------------------------------------------------------
# The audited sibling outside the worker: create-time voice auto-pick
# ---------------------------------------------------------------------------


_VOICE_YAML = (
    "schema_version: '1.0'\n"
    "identity:\n"
    "  name: Ally\n"
    "  role: warm companion\n"
    "  background: A warm, supportive friend.\n"
    "  language_default: en\n"
)


def test_a_free_owners_voice_auto_pick_runs_on_the_free_model(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same leak, one surface over: the owner-billed voice pick at persona create.

    Not named in the M4 docstring, but structurally identical — an owner-billed background
    LLM call resolved from ``app.state.tier_registry`` alone. The create request already
    binds the owner, so the plan read resolves here exactly as it does in a worker job.
    """
    import asyncio

    from persona_api.services import voice_assignment_service as vas

    _set_plan(engine, "free")
    paid, free = _RecordingBackend("paid-model"), _RecordingBackend("free-model")

    async def _catalogue(*_a: object, **_kw: object) -> object:
        return "cartesia", [
            vas._VoiceOption(voice_id="v1", name="Clara", gender="feminine", description="warm")
        ]  # noqa: SLF001, E501

    monkeypatch.setattr(vas, "_fetch_catalogue", _catalogue)
    monkeypatch.setattr(vas.persona_service, "set_voice", lambda **_k: None)
    state = SimpleNamespace(
        config=SimpleNamespace(voice_service_url="http://voice", voice_pick_tier=_TIER),
        tier_registry=_registry(paid),
        free_tier_registry=_registry(free),
        rls_engine=engine,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state), headers={})

    with _owner_scope(_OWNER):
        asyncio.run(
            vas.maybe_assign_voice(
                request,  # type: ignore[arg-type]
                owner_id=_OWNER,
                persona_id="p1",
                yaml_str=_VOICE_YAML,
            )
        )

    assert (free.calls, paid.calls) == (1, 0)


# ---------------------------------------------------------------------------
# The tripwire: the composition root cannot go back to resolving once
# ---------------------------------------------------------------------------


def test_the_worker_root_composes_no_background_backend_from_the_paid_registry() -> None:
    """Source tripwire over ``build_worker_registry`` (the R9-074 "required keyword" idea).

    Every background backend must come from ``plan_scoped_background_backend``; a bare
    ``tier_registry.get(...)`` is the defect, in any of the five places it appeared. This
    is a blunt instrument on purpose — the behavioural tests above prove the gate works,
    and this one stops the composition root from quietly growing a sixth ungated surface.
    """
    import inspect

    from persona_api.background.worker_root import build_worker_registry

    source = inspect.getsource(build_worker_registry)
    # Comments are excluded: the fix's own comments quote the old shape to explain it.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "tier_registry.get(" not in code
    assert code.count("plan_scoped_background_backend(") == 5  # the five background surfaces


def test_the_free_registry_is_a_required_argument_at_both_worker_entry_points() -> None:
    """ "Gating off" has to be stated, never defaulted (R9-074's ``credits_policy`` lesson).

    A keyword with a default is a keyword that gets forgotten, and here the forgotten value
    (``None`` = no gating) is the dangerous one. Both entry points therefore refuse to
    compose without it.
    """
    import inspect

    from persona_api.background.worker_root import build_worker_registry, start_in_process_worker

    for fn in (build_worker_registry, start_in_process_worker):
        param = inspect.signature(fn).parameters["free_tier_registry"]
        assert param.default is inspect.Parameter.empty, fn.__name__
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__
