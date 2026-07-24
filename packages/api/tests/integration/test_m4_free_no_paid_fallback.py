"""The free-tier no-paid-fallback gate (Spec M4, T5a) — the load-bearing safety test.

``RuntimeFactory._plan_tier_selection`` resolves the caller's plan and returns the
(tier_registry, preferred_backend_provider) the chat/agentic loop is CONSTRUCTED with.
Real construction (no mocks): real free + paid registries (dummy keys, no network) + a
real ``subscription`` read. The assertions inspect the CONCRETE resolved backend chain —
**this test FAILS if any paid provider is reachable in a free user's chain** — and prove
the preferred_model escape is disabled for free users, that a paid user still reaches the
paid tiers (discrimination), that community/absent-row behave correctly, and that an
unconfigured free registry fails closed (never a paid default).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.openrouter_passthrough import build_openrouter_passthrough
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.errors import TierNotConfiguredError
from persona_runtime.tier import free_tier_registry_from_env, tier_registry_from_env
from persona_voice.agent.runner import _load_plan_code, _select_voice_tier_registry
from persona_voice.config import VoiceConfig
from sqlalchemy import text

if TYPE_CHECKING:
    from persona_runtime.tier import TierRegistry
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_PAID_PROVIDERS = {"anthropic", "openai"}


class _FakeEmbedder:
    model_name = "fake"
    dimension = 384


@pytest.fixture(autouse=True)
def _keys_and_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy keys + paid (anthropic/openai) & free (openrouter :free) tier env (autouse).

    The fail-closed test further deletes the ``PERSONA_FREE_*`` vars via its own monkeypatch.
    """
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-dummy")
    monkeypatch.setenv("PERSONA_ANTHROPIC_API_KEY", "sk-ant-dummy")
    monkeypatch.setenv("PERSONA_OPENAI_API_KEY", "sk-oai-dummy")
    monkeypatch.setenv("PERSONA_FRONTIER_MODELS", "anthropic/claude-sonnet-4-6,openai/gpt-4o")
    monkeypatch.setenv("PERSONA_MID_MODELS", "anthropic/claude-3-5-haiku,openai/gpt-4o-mini")
    monkeypatch.setenv(
        "PERSONA_FREE_FRONTIER_MODELS", "openrouter/free-a:free,openrouter/free-b:free"
    )
    monkeypatch.setenv(
        "PERSONA_FREE_MID_MODELS", "openrouter/free-m-a:free,openrouter/free-m-b:free"
    )


def _paid() -> TierRegistry:
    return tier_registry_from_env()


def _free() -> TierRegistry:
    return free_tier_registry_from_env()


def _factory(engine: Engine, *, paid: TierRegistry, free: TierRegistry | None) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=engine,
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=paid,
        free_tier_registry=free,
        turn_log_writer=object(),  # type: ignore[arg-type] — never touched by _plan_tier_selection
        audit_root=Path("/tmp/persona-audit-test"),
    )


def _seed_sub(engine: Engine, uid: str, plan_code: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO subscription (user_id, plan_code) VALUES (:u, :p) "
                "ON CONFLICT (user_id) DO UPDATE SET plan_code = :p"
            ),
            {"u": uid, "p": plan_code},
        )


def _chain(registry: TierRegistry, tier: str) -> list[tuple[str, str]]:
    backend = registry.get(tier)
    if isinstance(backend, MultiModelChatBackend):
        return [(b.provider_name, b.model_name) for b in backend.backends]
    return [(backend.provider_name, backend.model_name)]


def test_free_user_resolves_free_only_chain_with_no_preferred(migrated_engine: Engine) -> None:
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_free", "free")
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_free")
    try:
        registry, preferred = factory._plan_tier_selection()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is free
    assert preferred is None  # the preferred_model override is DISABLED — no escape hatch
    # THE load-bearing assertion: the free user's resolved chain has ZERO paid providers.
    for tier in ("frontier", "mid"):
        providers = {p for p, _ in _chain(registry, tier)}
        assert providers.isdisjoint(_PAID_PROVIDERS), f"paid provider reachable in free {tier}!"
        assert providers == {"openrouter"}


def test_free_user_preferred_model_paid_still_resolves_free_only(migrated_engine: Engine) -> None:
    """Even a free persona with ``preferred_model='anthropic/...'`` can't escape: the gate
    returns ``preferred=None``, so ``preferred_model`` is inert (loop.py: a ``None`` provider
    short-circuits the override) and the chain stays the free-only registry."""
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_free", "free")
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_free")
    try:
        registry, preferred = factory._plan_tier_selection()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    # preferred is None regardless of any persona's preferred_model → paid id can never front.
    assert preferred is None
    assert registry is free
    assert all(p == "openrouter" for p, _ in _chain(registry, "frontier"))


def test_paid_user_resolves_paid_registry_and_passthrough(migrated_engine: Engine) -> None:
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_pro", "pro")
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_pro")
    try:
        registry, preferred = factory._plan_tier_selection()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is paid
    assert preferred is build_openrouter_passthrough  # non-None → the gate DISCRIMINATES
    # The paid tier legitimately reaches paid providers (contrast with free).
    assert any(p in _PAID_PROVIDERS for p, _ in _chain(registry, "frontier"))


def test_absent_subscription_row_defaults_to_free(migrated_engine: Engine) -> None:
    """A user with no subscription row is treated as FREE (fail-safe — restrictive)."""
    paid, free = _paid(), _free()
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_no_sub_row")
    try:
        registry, preferred = factory._plan_tier_selection()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is free
    assert preferred is None


def test_community_no_free_registry_is_byte_identical(migrated_engine: Engine) -> None:
    """Community (free_tier_registry None) → every user resolves the paid tiers + passthrough."""
    paid = _paid()
    _seed_sub(migrated_engine, "user_free", "free")
    factory = _factory(migrated_engine, paid=paid, free=None)  # community / gating off

    token = current_user_id.set("user_free")
    try:
        registry, preferred = factory._plan_tier_selection()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is paid  # no gating — a "free" user still gets the paid tiers in community
    assert preferred is build_openrouter_passthrough


def test_unconfigured_free_registry_fails_closed_never_paid(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``PERSONA_FREE_*_MODELS`` → the free registry is EMPTY → a free user's tier lookup
    raises (→ graceful T5b), NEVER a paid default."""
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-dummy")
    monkeypatch.setenv("PERSONA_ANTHROPIC_API_KEY", "sk-ant-dummy")
    monkeypatch.setenv("PERSONA_FRONTIER_MODELS", "anthropic/claude-sonnet-4-6")
    for var in (
        "PERSONA_FREE_FRONTIER_MODELS",
        "PERSONA_FREE_MID_MODELS",
        "PERSONA_FREE_SMALL_MODELS",
    ):
        monkeypatch.delenv(var, raising=False)
    paid, free = _paid(), _free()  # free is EMPTY (fail-closed)
    _seed_sub(migrated_engine, "user_free", "free")
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_free")
    try:
        registry, preferred = factory._plan_tier_selection()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is free
    assert preferred is None
    with pytest.raises(TierNotConfiguredError):
        registry.get("frontier")  # fail-closed: no paid fallback, ever


# ---------------------------------------------------------------------------
# T5c — background get-sites (consolidation / recognition / initiative-scan / title)
# route every LLM through ``RuntimeFactory._plan_tier_registry()``, the plan-scoped twin
# of ``_plan_tier_selection`` — so a FREE owner's background LLM stays free-only.
# ---------------------------------------------------------------------------


def test_background_free_owner_resolves_free_only_registry(migrated_engine: Engine) -> None:
    """A free owner's background LLM (``_plan_tier_registry``) is the free-only registry — every
    tier it can touch (background/recognition/scan/title all resolve small→mid→frontier) is
    zero-paid. This test FAILS if any paid provider is reachable on the background path."""
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_free", "free")
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_free")
    try:
        registry = factory._plan_tier_registry()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is free
    for tier in ("frontier", "mid", "small"):  # the whole fallback walk any get-site can hit
        providers = {p for p, _ in _chain(registry, tier)}
        assert providers.isdisjoint(_PAID_PROVIDERS), f"paid provider reachable in bg {tier}!"
        assert providers == {"openrouter"}


def test_background_paid_owner_resolves_paid_registry(migrated_engine: Engine) -> None:
    """Discrimination: a paid owner's background LLM stays on the paid registry."""
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_pro", "pro")
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_pro")
    try:
        registry = factory._plan_tier_registry()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is paid
    assert any(p in _PAID_PROVIDERS for p, _ in _chain(registry, "frontier"))


def test_background_unconfigured_free_owner_fails_closed(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``PERSONA_FREE_*`` → a free owner's background registry is EMPTY → the get-site raises,
    NEVER a paid default (fail-closed, same as chat)."""
    for var in (
        "PERSONA_FREE_FRONTIER_MODELS",
        "PERSONA_FREE_MID_MODELS",
        "PERSONA_FREE_SMALL_MODELS",
    ):
        monkeypatch.delenv(var, raising=False)
    paid, free = _paid(), _free()  # free EMPTY
    _seed_sub(migrated_engine, "user_free", "free")
    factory = _factory(migrated_engine, paid=paid, free=free)

    token = current_user_id.set("user_free")
    try:
        registry = factory._plan_tier_registry()  # noqa: SLF001
    finally:
        current_user_id.reset(token)

    assert registry is free
    with pytest.raises(TierNotConfiguredError):
        registry.get("mid")  # background generation tier — no paid fallback


# ---------------------------------------------------------------------------
# T5c — voice (out-of-process). ``_select_voice_tier_registry`` is the voice twin of chat's
# ``_plan_tier_selection``: it resolves the owner's plan (raw ``subscription`` read across the
# process boundary) and swaps to the free-only registry for a free caller. Voice wires no
# ``preferred_backend_provider``, so the registry swap makes the WHOLE voice LLM chain
# (generation ``mid`` + summariser/gate ``small``) free-only — no paid fallback anywhere.
# ---------------------------------------------------------------------------


def _voice_cfg(edition: str) -> VoiceConfig:
    return VoiceConfig(edition=edition)  # edition is the only field read by is_cloud


def test_voice_free_caller_resolves_free_only_chain(migrated_engine: Engine) -> None:
    """A free caller (cloud + plan_code 'free') → the free registry; generation (``mid``) and
    summariser/gate (``small``) both resolve free-only. FAILS if any paid provider is reachable."""
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_free", "free")

    resolved = _select_voice_tier_registry(
        config=_voice_cfg("cloud"),
        tier_registry=paid,
        free_tier_registry=free,
        rls_engine=migrated_engine,
        user_id="user_free",
    )

    assert resolved is free
    for tier in ("mid", "small"):  # the two tiers a voice call actually walks
        providers = {p for p, _ in _chain(resolved, tier)}
        assert providers.isdisjoint(_PAID_PROVIDERS), f"paid provider reachable in voice {tier}!"
        assert providers == {"openrouter"}


def test_voice_paid_caller_resolves_paid_registry(migrated_engine: Engine) -> None:
    """Discrimination: a paid caller stays on the paid registry (reaches paid providers)."""
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_pro", "pro")

    resolved = _select_voice_tier_registry(
        config=_voice_cfg("cloud"),
        tier_registry=paid,
        free_tier_registry=free,
        rls_engine=migrated_engine,
        user_id="user_pro",
    )

    assert resolved is paid
    assert any(p in _PAID_PROVIDERS for p, _ in _chain(resolved, "mid"))


def test_voice_absent_row_defaults_free(migrated_engine: Engine) -> None:
    """No subscription row → fail-safe 'free' → the free registry (restrictive)."""
    paid, free = _paid(), _free()

    resolved = _select_voice_tier_registry(
        config=_voice_cfg("cloud"),
        tier_registry=paid,
        free_tier_registry=free,
        rls_engine=migrated_engine,
        user_id="user_voice_no_row",
    )

    assert resolved is free
    assert _load_plan_code(migrated_engine, "user_voice_no_row") == "free"  # fail-safe default


def test_voice_community_no_free_registry_is_paid(migrated_engine: Engine) -> None:
    """Community (``free_tier_registry`` None) → paid registry for every caller (byte-identical)."""
    paid = _paid()
    _seed_sub(migrated_engine, "user_free", "free")

    resolved = _select_voice_tier_registry(
        config=_voice_cfg("cloud"),
        tier_registry=paid,
        free_tier_registry=None,  # community / gating off
        rls_engine=migrated_engine,
        user_id="user_free",
    )

    assert resolved is paid


def test_voice_not_cloud_edition_is_paid(migrated_engine: Engine) -> None:
    """Even with a free registry present, a non-cloud edition → paid (the ``is_cloud`` gate)."""
    paid, free = _paid(), _free()
    _seed_sub(migrated_engine, "user_free", "free")

    resolved = _select_voice_tier_registry(
        config=_voice_cfg("community"),
        tier_registry=paid,
        free_tier_registry=free,
        rls_engine=migrated_engine,
        user_id="user_free",
    )

    assert resolved is paid


def test_voice_unconfigured_free_caller_fails_closed(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``PERSONA_FREE_*`` → a free caller's voice registry is EMPTY → tier get raises, NEVER a
    paid default (fail-closed across the process boundary)."""
    for var in (
        "PERSONA_FREE_FRONTIER_MODELS",
        "PERSONA_FREE_MID_MODELS",
        "PERSONA_FREE_SMALL_MODELS",
    ):
        monkeypatch.delenv(var, raising=False)
    paid, free = _paid(), _free()  # free EMPTY
    _seed_sub(migrated_engine, "user_free", "free")

    resolved = _select_voice_tier_registry(
        config=_voice_cfg("cloud"),
        tier_registry=paid,
        free_tier_registry=free,
        rls_engine=migrated_engine,
        user_id="user_free",
    )

    assert resolved is free
    with pytest.raises(TierNotConfiguredError):
        resolved.get("mid")  # voice generation tier — no paid fallback, ever
