"""A connector turn is SELECTED and BILLED exactly like a web-chat turn (R9-074 + R9-079).

The owner ruling is parity: a message sent on Telegram / Discord / Slack / WhatsApp /
SMS / email must pick the same model and cost the same money as the identical message
typed into web chat. Two independent defects broke that, and both failed OPEN — the
dangerous direction — because every relevant argument is keyword-only with a default:

* **R9-074** — the connector never passed ``free_tier_registry``, and
  ``RuntimeFactory._plan_tier_selection`` reads ``None`` as *"plan gating is off"*, so a
  FREE-plan user was handed the PAID tiers on every connector.
* **R9-079** — the connector constructed ``ChatTurnRegistry(sink=…, rls_engine=…)`` and
  nothing else, so ``credits_policy`` defaulted to ``None`` (= bill nothing) and every
  connector turn was free.

The load-bearing test here is :func:`test_the_connector_and_the_api_compose_identical_
chat_turn_billing_inputs`: it drives BOTH real composition roots — the api's own
lifespan and the connector's ``build_reply_runner`` — over the same ``APIConfig`` and
compares the billing inputs each one actually handed to ``ChatTurnRegistry``. Asserting
"a policy was passed" would prove nothing; the config used here deliberately carries
NON-default billing numbers, so a surface that silently fell back to the constructor
defaults fails.

The gating half is proven behaviourally, not structurally: a free-plan owner is run
through the real ``_plan_tier_selection`` against a real subscription row and must come
out on the FREE registry with the ``preferred_model`` escape hatch disabled — and the
pre-fix shape (no free registry) is asserted to do the opposite, so the test would still
fail if the fix were reverted.
"""

from __future__ import annotations

import contextlib
import pathlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import persona_connectors
import pytest
from fastapi.testclient import TestClient
from persona.backends import BackendConfig
from persona_api.app import create_app
from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.config import APIConfig, Edition
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import subscription as subscription_t
from persona_api.editions.credits_policy import MeteredCreditsPolicy, UnlimitedCreditsPolicy
from persona_api.editions.factory import build_credits_policy, build_stripe_gateway
from persona_api.jobs import JobQueue
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.model_tiers import build_free_tier_registry
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.task_steering_service import TaskSteeringService
from persona_api.services.turn_log_writer import PostgresTurnLogWriter
from persona_api.services.verb_service_composition import build_conversational_verb_services
from persona_api.tasks.store import TaskStore
from persona_connectors.composition import build_reply_runner
from persona_runtime.tier import TierConfig, TierRegistry
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_alice"

#: Deliberately NON-default so a surface that fell back to ``ChatTurnRegistry``'s own
#: defaults (1 / True / 500) is caught rather than accidentally agreeing.
_CREDITS_PER_TURN = 7
_PROPORTIONAL_CREDITS = False
_MAX_TURN_CREDITS = 42


def _config(tmp_path: Path, *, edition: Edition = Edition.community) -> APIConfig:
    """An APIConfig with every filesystem root under ``tmp_path`` and odd billing numbers."""
    return APIConfig(
        edition=edition,
        community_db_path=str(tmp_path / "community.db"),
        community_memory_path=str(tmp_path / "memory"),
        audit_root=str(tmp_path / "audit"),
        workspace_root=str(tmp_path / "workspaces"),
        credits_per_turn=_CREDITS_PER_TURN,
        proportional_credits=_PROPORTIONAL_CREDITS,
        max_turn_credits=_MAX_TURN_CREDITS,
    )


class _RegistryRecorder:
    """Stands in for ``ChatTurnRegistry`` and records how each surface constructed it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> ChatTurnRegistry:  # noqa: ANN401 — verbatim capture
        self.calls.append(dict(kwargs))
        return ChatTurnRegistry(**kwargs)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RegistryRecorder:
    """Capture every ``ChatTurnRegistry`` construction that goes through the shared builder."""
    rec = _RegistryRecorder()
    monkeypatch.setattr("persona_api.services.chat_turn_composition.ChatTurnRegistry", rec)
    return rec


def _billing_shape(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The comparable billing identity of one registry construction.

    Policies / gateways / queues are collaborators with no ``__eq__``, so they compare by
    TYPE (which is what the edition seam actually decides); the numbers and the credit
    formula config compare by value.
    """

    def _type_name(value: object) -> str | None:
        return type(value).__name__ if value is not None else None

    return {
        "credits_policy": _type_name(kwargs["credits_policy"]),
        "gateway": _type_name(kwargs["gateway"]),
        "credits_per_turn": kwargs["credits_per_turn"],
        "proportional_credits": kwargs["proportional_credits"],
        "max_turn_credits": kwargs["max_turn_credits"],
        "billing_config": kwargs["billing_config"],
        "job_queue": _type_name(kwargs["job_queue"]),
    }


class _FakeRuntimeFactory:
    """The heavy api ``RuntimeFactory`` is the deploy seam; the runner only needs the seam."""

    async def build_conversation_loop(self, persona_id: str) -> object:
        raise AssertionError(f"no turn is run in these tests (persona_id={persona_id})")


@contextlib.contextmanager
def _owner_scope(owner_id: str) -> Iterator[None]:
    token = current_user_id.set(owner_id)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _connector_engine(tmp_path: Path) -> Engine:
    engine = make_community_engine(tmp_path / "connectors.db")
    create_community_schema(engine)
    ensure_owner(engine, owner_id=_OWNER, email="alice@example.com")
    return engine


def _build_connector_runner(config: APIConfig, engine: Engine) -> None:
    """Compose the connector reply runner exactly as ``__main__._amain`` does.

    R9-081: the verb services are built here the same way the service entry builds
    them. If this drifted from ``_amain`` the wiring assertions below would pass
    while production stayed unwired, which is the exact failure they exist to catch.
    """
    verb_services = build_conversational_verb_services(rls_engine=engine, config=config)
    build_reply_runner(
        runtime_factory=_FakeRuntimeFactory(),  # type: ignore[arg-type]
        rls_engine=engine,
        owner_scope=_owner_scope,
        api_config=config,
        credits_policy=build_credits_policy(config),
        gateway=build_stripe_gateway(config),
        job_queue=JobQueue(engine),
        task_steering_service=TaskSteeringService(tasks=TaskStore(engine)),
        task_reschedule_service=verb_services.reschedule,
        initiative_verb_service=verb_services.initiative,
    )


# ---------------------------------------------------------------------------
# R9-079 — billing parity
# ---------------------------------------------------------------------------


def test_the_connector_and_the_api_compose_identical_chat_turn_billing_inputs(
    tmp_path: Path, recorder: _RegistryRecorder
) -> None:
    """THE parity assertion: both composition roots hand the registry the same billing set.

    The api half runs its REAL lifespan (``create_app`` under ``TestClient``); the connector
    half runs its REAL ``build_reply_runner``. Both are given the same ``APIConfig``, and the
    two captured constructions are compared field by field — not "something was passed".
    """
    config = _config(tmp_path)

    with TestClient(create_app(config)):
        pass  # the real lifespan composed app.state.chat_turn_registry
    _build_connector_runner(config, _connector_engine(tmp_path))

    # EXACTLY two constructions, one per surface. This is load-bearing: if either root
    # went around the shared builder (as the connector used to, calling ``ChatTurnRegistry``
    # directly), only one call would be recorded and the comparison below would silently
    # degrade into comparing a surface with itself.
    assert len(recorder.calls) == 2, recorder.calls
    api_call, connector_call = recorder.calls

    assert _billing_shape(connector_call) == _billing_shape(api_call)
    # ...and the shared numbers really came from the config, not the constructor defaults
    # (which are 1 / True / 500 — the values a forgotten keyword would have produced).
    assert _billing_shape(connector_call)["credits_per_turn"] == _CREDITS_PER_TURN
    assert _billing_shape(connector_call)["proportional_credits"] is _PROPORTIONAL_CREDITS
    assert _billing_shape(connector_call)["max_turn_credits"] == _MAX_TURN_CREDITS


def test_a_cloud_connector_turn_is_metered_like_a_cloud_web_turn(
    tmp_path: Path, recorder: _RegistryRecorder
) -> None:
    """R9-079 regression guard: cloud connectors meter through the METERED policy.

    Pre-fix the registry got ``credits_policy=None`` — the "bill nothing" default — so this
    is the assertion that would have failed on the shipped code.
    """
    config = _config(tmp_path, edition=Edition.cloud)
    _build_connector_runner(config, _connector_engine(tmp_path))

    assert len(recorder.calls) == 1, recorder.calls  # it went through the SHARED builder
    call = recorder.calls[0]
    assert call["credits_policy"] is not None
    assert isinstance(call["credits_policy"], MeteredCreditsPolicy)
    assert call["job_queue"] is not None  # the turn-boundary synthesis enqueue, as in api


def test_a_community_connector_turn_stays_unbilled(
    tmp_path: Path, recorder: _RegistryRecorder
) -> None:
    """Community (self-host) must stay unbilled — but by an EXPLICIT unlimited policy.

    "Unbilled" is now a stated decision (``build_credits_policy`` returns the unlimited
    policy for community) rather than a forgotten keyword, which is the whole point of
    making the argument required.
    """
    config = _config(tmp_path, edition=Edition.community)
    _build_connector_runner(config, _connector_engine(tmp_path))

    assert len(recorder.calls) == 1, recorder.calls  # it went through the SHARED builder
    call = recorder.calls[0]
    assert isinstance(call["credits_policy"], UnlimitedCreditsPolicy)
    assert call["gateway"] is None  # no Stripe anywhere near a self-host install


def test_the_connector_wires_the_verbs_it_can_actually_apply(
    tmp_path: Path, recorder: _RegistryRecorder
) -> None:
    """R9-081 THE regression: a verb the runtime emits must reach a worker that applies it.

    The connector's ``RuntimeFactory`` builds the loop-side interpreters
    unconditionally, so a persona confirms a reschedule or a steering verb over
    Telegram whatever this worker holds. With these at ``None`` the confirmation was
    dropped and the user still read "Done, I've set that up" -- a silent lie rather
    than an error, because the reply text is produced before the worker call and
    independently of it.
    """
    config = _config(tmp_path)
    _build_connector_runner(config, _connector_engine(tmp_path))

    assert len(recorder.calls) == 1, recorder.calls  # it went through the SHARED builder
    call = recorder.calls[0]
    assert call["task_steering_service"] is not None, "pause / resume / cancel go nowhere"
    assert call["task_reschedule_service"] is not None, "a confirmed reschedule goes nowhere"


def test_the_connector_leaves_origination_unwired_deliberately(
    tmp_path: Path, recorder: _RegistryRecorder
) -> None:
    """Origination alone stays a STATED gap, and the reason is specific to it.

    Its failure notifier narrates "I could not create that after all" through the C0
    delivery seam to an open web tab, which a connector process does not have, so a
    failed origination would be persisted and never seen. That needs a decision about
    where a connector-raised failure account is delivered, not a wiring change. This
    pins the boundary so closing it has to be deliberate.
    """
    config = _config(tmp_path)
    _build_connector_runner(config, _connector_engine(tmp_path))

    assert recorder.calls[0]["origination_service"] is None


# ---------------------------------------------------------------------------
# R9-074 — plan-gated model selection
# ---------------------------------------------------------------------------


def _tier_registry(model: str) -> TierRegistry:
    return TierRegistry(
        {
            "mid": TierConfig(
                name="mid",
                backend_config=BackendConfig(provider="anthropic", model=model, api_key="k"),
            )
        }
    )


def _runtime_factory(
    engine: Engine, tmp_path: Path, *, free_tier_registry: TierRegistry | None
) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=engine,
        embedder=object(),  # type: ignore[arg-type] — never used by _plan_tier_selection
        tier_registry=_tier_registry("paid-model"),
        free_tier_registry=free_tier_registry,
        turn_log_writer=PostgresTurnLogWriter(engine),
        audit_root=tmp_path / "audit",
    )


def _set_plan(engine: Engine, plan_code: str) -> None:
    with engine.begin() as conn:
        conn.execute(insert(subscription_t).values(user_id=_OWNER, plan_code=plan_code))


def test_a_free_plan_owner_on_the_connector_path_resolves_the_free_registry(
    tmp_path: Path,
) -> None:
    """THE gating assertion: a free-plan owner gets the FREE tiers and no escape hatch.

    Runs the real ``_plan_tier_selection`` against a real ``subscription`` row, in the
    owner scope the connector's ``owner_scope`` binds — the same contextvar the api's
    request path binds. ``preferred_backend_provider`` must be ``None`` too, else
    ``preferred_model`` would let a free user reach a paid model anyway.
    """
    engine = _connector_engine(tmp_path)
    _set_plan(engine, "free")
    free = _tier_registry("free-model")
    factory = _runtime_factory(engine, tmp_path, free_tier_registry=free)

    with _owner_scope(_OWNER):
        registry, preferred_provider = factory._plan_tier_selection()  # noqa: SLF001

    assert registry is free
    assert preferred_provider is None


def test_a_paid_plan_owner_on_the_connector_path_still_resolves_the_paid_registry(
    tmp_path: Path,
) -> None:
    """Gating must not over-reach: a paying customer keeps the paid tiers + the override."""
    engine = _connector_engine(tmp_path)
    _set_plan(engine, "pro")
    paid = _tier_registry("paid-model")
    factory = RuntimeFactory(
        rls_engine=engine,
        embedder=object(),  # type: ignore[arg-type]
        tier_registry=paid,
        free_tier_registry=_tier_registry("free-model"),
        turn_log_writer=PostgresTurnLogWriter(engine),
        audit_root=tmp_path / "audit",
    )

    with _owner_scope(_OWNER):
        registry, preferred_provider = factory._plan_tier_selection()  # noqa: SLF001

    assert registry is paid
    assert preferred_provider is not None


def test_the_pre_fix_connector_shape_would_have_served_a_free_owner_the_paid_tiers(
    tmp_path: Path,
) -> None:
    """The counterfactual that makes the fix falsifiable (R9-074).

    Omitting ``free_tier_registry`` — literally what the connector used to do — hands a
    free-plan owner the PAID registry AND re-enables the ``preferred_model`` passthrough.
    If someone drops the argument again, the fix's own test above starts failing while
    this one keeps passing, which is exactly the signal we want.
    """
    engine = _connector_engine(tmp_path)
    _set_plan(engine, "free")
    factory = _runtime_factory(engine, tmp_path, free_tier_registry=None)

    with _owner_scope(_OWNER):
        registry, preferred_provider = factory._plan_tier_selection()  # noqa: SLF001

    assert registry is factory._tier_registry  # noqa: SLF001 — the PAID tiers
    assert preferred_provider is not None


def test_plan_gating_is_armed_in_cloud_and_off_in_community(tmp_path: Path) -> None:
    """``build_free_tier_registry`` is the ONE definition both composition roots call.

    ``None`` and "empty" mean opposite things — gating OFF vs gating ON with nothing
    configured — so the edition condition has to live in one shared place rather than
    being retyped (or forgotten) at each root.
    """
    cloud = build_free_tier_registry(
        _config(tmp_path, edition=Edition.cloud), openrouter_subscription_mode=None
    )
    community = build_free_tier_registry(
        _config(tmp_path, edition=Edition.community), openrouter_subscription_mode=None
    )

    assert cloud is not None  # armed — fail-closed even when PERSONA_FREE_* is unset
    assert community is None  # ungated, byte-identical to the pre-M4 self-host path


def test_the_connector_runtime_factory_arms_gating_and_threads_the_openrouter_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``__main__`` wiring itself (R9-074), with the heavy deploy seam stubbed out.

    ``_build_runtime_factory`` loads torch + real model backends, so the embedder / tier
    builders are replaced with recorders; what is under test is the ARGUMENTS the entry
    point passes — the free registry being present at all, and the resolved OpenRouter
    mode reaching the paid tier builder (it previously reached neither).
    """
    from persona_connectors import __main__ as service

    recorded: dict[str, Any] = {}

    def _fake_runtime_factory(**kwargs: Any) -> object:  # noqa: ANN401 — verbatim capture
        recorded.update(kwargs)
        # R9-125: the builder now enables the graph store on the factory it returns.
        return SimpleNamespace(enable_graph_writes=lambda **_kw: None)

    def _fake_tier_registry_from_env(*, openrouter_subscription_mode: Any) -> object:  # noqa: ANN401
        recorded["paid_registry_mode"] = openrouter_subscription_mode
        return object()

    monkeypatch.setattr(service, "RuntimeFactory", _fake_runtime_factory)
    monkeypatch.setattr(service, "resolve_openrouter_subscription_mode", lambda: "free")
    monkeypatch.setattr(service.persona_service, "default_embedder", lambda _model: object())
    monkeypatch.setattr(service, "tier_registry_from_env", _fake_tier_registry_from_env)

    config = _config(tmp_path, edition=Edition.cloud)
    engine = _connector_engine(tmp_path)
    service._build_runtime_factory(  # noqa: SLF001
        config, engine, credits_policy=build_credits_policy(config)
    )

    assert recorded["free_tier_registry"] is not None  # gating ARMED on the connector
    assert recorded["paid_registry_mode"] == "free"  # the mode reaches the paid builder too
    assert isinstance(recorded["credits_policy"], MeteredCreditsPolicy)


def test_the_service_entry_actually_passes_the_verb_services() -> None:
    """The wiring test above builds the services itself, so it cannot see this.

    ``_build_connector_runner`` calls ``build_reply_runner`` directly. That proves the
    forwarding, but a green suite would still be compatible with ``__main__._amain``
    never passing them -- the fix would be unreachable in production while every test
    passed. This reads the real call site, in the spirit of the A10-D-9 grep guard.
    """
    entry = pathlib.Path(persona_connectors.__file__).parent / "__main__.py"
    source = entry.read_text()
    # Slice to the call's own closing paren (a bare ")" at the call's indent), not the
    # first ")" -- nested calls like build_stripe_gateway(api_config) sit inside it.
    call = source.split("run_turn = build_reply_runner(", 1)[1].split("\n    )", 1)[0]
    for kwarg in (
        "task_steering_service=",
        "task_reschedule_service=",
        "initiative_verb_service=",
    ):
        assert kwarg in call, f"the service entry never passes {kwarg!r}; the wiring is dead code"
