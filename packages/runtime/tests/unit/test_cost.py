"""Unit tests for persona_runtime.cost (Spec M2, M2-T1; D-M2-1).

Replaces the deleted ``_PRICE_TABLE`` tests (``test_nvidia_price_table.py`` +
``test_logging.py::TestCostEstimation``): the estimator is now the Spec-22/23
metadata resolver chain, and these tests pin

* the canonical-id key shapes per provider (bare anthropic / prefixed nvidia /
  openrouter slug — the Spec-25 §2.6 silent-miss lesson carried forward),
* coverage parity: every key the deleted ``_PRICE_TABLE`` served still
  resolves through the static chain (the M2-T1 F4 rows),
* provenance → basis mapping (static / catalog / unpriced / actual),
* the no-fetch turn-path invariant (``allow_fetch=False`` never touches a
  fetching resolver), and
* honest degrade: unknown model → ``(0.0, "unpriced")``, warn-once, stable.
"""

from __future__ import annotations

import pytest
from persona.backends.metadata import ChainedModelMetadataResolver
from persona.backends.model_metadata import ModelMetadata
from persona_runtime.cost import compute_turn_cost

_META = ModelMetadata(
    cost_input_per_1k_tokens=0.30,
    cost_output_per_1k_tokens=1.50,
    latency_p50_ms=100.0,
    quality_benchmark=0.9,
    tools_supported=True,
    vision_supported=True,
    context_length=200_000,
)


class _RecordingSource:
    """A CostSource fake that records the looked-up id and answers from a map."""

    def __init__(self, hits: dict[str, tuple[ModelMetadata, str]] | None = None) -> None:
        self.hits = hits or {}
        self.calls: list[tuple[str, bool]] = []

    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, str] | None:
        self.calls.append((model_id, allow_fetch))
        return self.hits.get(model_id)


class _StaticOnly:
    """A static-link fake for chain-composition tests."""

    def __init__(self, hits: dict[str, ModelMetadata]) -> None:
        self.hits = hits

    def resolve(self, model_id: str) -> ModelMetadata | None:
        return self.hits.get(model_id)


class _FetchingCatalog:
    """An OpenRouter-link fake WITH a no-fetch arm; ``resolve`` counts as a fetch."""

    def __init__(self, hits: dict[str, ModelMetadata], *, warm: bool) -> None:
        self.hits = hits
        self.warm = warm
        self.fetch_calls = 0

    def resolve(self, model_id: str) -> ModelMetadata | None:
        self.fetch_calls += 1  # a cold resolve() would hit the network
        return self.hits.get(model_id)

    def resolve_no_fetch(self, model_id: str) -> ModelMetadata | None:
        if not self.warm:
            return None
        return self.hits.get(model_id)


class _FetchOnlyCatalog:
    """A resolver with NO ``resolve_no_fetch`` — cannot promise fetch-free lookup."""

    def __init__(self) -> None:
        self.fetch_calls = 0

    def resolve(self, model_id: str) -> ModelMetadata | None:  # noqa: ARG002
        self.fetch_calls += 1
        return _META


class TestCanonicalIdShapes:
    """The provider-specific model-id shapes all land on the right lookup key."""

    def test_bare_model_gains_provider_prefix(self) -> None:
        source = _RecordingSource()
        compute_turn_cost(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=1,
            completion_tokens=1,
            source=source,
        )
        assert source.calls == [("anthropic/claude-sonnet-4-6", False)]

    def test_prefixed_model_kept_as_is(self) -> None:
        # NVIDIA catalog ids arrive already prefixed (Spec 25 §2.6).
        source = _RecordingSource()
        compute_turn_cost(
            provider="nvidia",
            model="nvidia/llama-3.3-nemotron-super-49b-v1.5",
            prompt_tokens=1,
            completion_tokens=1,
            source=source,
        )
        assert source.calls == [("nvidia/llama-3.3-nemotron-super-49b-v1.5", False)]

    def test_openrouter_slug_kept_as_is(self) -> None:
        # The OR model string IS the catalog slug — never openrouter/-prefixed.
        source = _RecordingSource()
        compute_turn_cost(
            provider="openrouter",
            model="z-ai/glm-4.6",
            prompt_tokens=1,
            completion_tokens=1,
            source=source,
        )
        assert source.calls == [("z-ai/glm-4.6", False)]


class TestBasisMapping:
    def test_static_hit_is_estimate_static(self) -> None:
        source = _RecordingSource({"anthropic/claude-sonnet-4-6": (_META, "static")})
        cents, basis = compute_turn_cost(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=1000,
            completion_tokens=500,
            source=source,
        )
        assert basis == "estimate_static"
        assert cents == pytest.approx(0.30 + 1.50 * 0.5)

    def test_catalog_hit_is_estimate_catalog(self) -> None:
        source = _RecordingSource({"z-ai/glm-4.6": (_META, "catalog")})
        _, basis = compute_turn_cost(
            provider="openrouter",
            model="z-ai/glm-4.6",
            prompt_tokens=10,
            completion_tokens=10,
            source=source,
        )
        assert basis == "estimate_catalog"

    def test_actual_overrides_estimate(self) -> None:
        # A response-side actual is authoritative even when the chain would hit.
        source = _RecordingSource({"anthropic/claude-sonnet-4-6": (_META, "static")})
        cents, basis = compute_turn_cost(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=1000,
            completion_tokens=500,
            actual_cost_usd=0.0123,
            source=source,
        )
        assert basis == "actual_openrouter"
        assert cents == pytest.approx(1.23)
        assert source.calls == []  # the chain was never consulted

    def test_actual_zero_is_a_valid_actual(self) -> None:
        # A ``:free`` OpenRouter route reports cost 0.0 — that IS the actual.
        cents, basis = compute_turn_cost(
            provider="openrouter",
            model="some/free-model:free",
            prompt_tokens=100,
            completion_tokens=100,
            actual_cost_usd=0.0,
            source=_RecordingSource(),
        )
        assert (cents, basis) == (0.0, "actual_openrouter")

    def test_negative_actual_treated_as_absent(self) -> None:
        # Defensive: a negative "cost" is not a payment; fall to the chain.
        source = _RecordingSource({"anthropic/claude-sonnet-4-6": (_META, "static")})
        _, basis = compute_turn_cost(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=10,
            completion_tokens=10,
            actual_cost_usd=-0.5,
            source=source,
        )
        assert basis == "estimate_static"

    def test_actual_rounds_to_micro_cents(self) -> None:
        cents, _ = compute_turn_cost(
            provider="openrouter",
            model="a/b",
            prompt_tokens=1,
            completion_tokens=1,
            actual_cost_usd=0.000001234567,
            source=_RecordingSource(),
        )
        assert cents == pytest.approx(round(0.0001234567, 6))


class TestUnpricedDegrade:
    def test_unknown_model_is_unpriced_zero_and_stable(self) -> None:
        # Two calls with the same unknown pair both degrade identically; the
        # once-only warning guard must not change the return value.
        first = compute_turn_cost(
            provider="acme",
            model="mystery-model-v9",
            prompt_tokens=1000,
            completion_tokens=1000,
            source=_RecordingSource(),
        )
        second = compute_turn_cost(
            provider="acme",
            model="mystery-model-v9",
            prompt_tokens=1000,
            completion_tokens=1000,
            source=_RecordingSource(),
        )
        assert first == (0.0, "unpriced")
        assert second == (0.0, "unpriced")


class TestNoFetchInvariant:
    """The turn path must never trigger a catalog fetch (M2 §2)."""

    def test_cold_catalog_is_a_miss_not_a_fetch(self) -> None:
        catalog = _FetchingCatalog({"long/tail-model": _META}, warm=False)
        chain = ChainedModelMetadataResolver(static=_StaticOnly({}), openrouter=catalog)
        result = compute_turn_cost(
            provider="openrouter",
            model="long/tail-model",
            prompt_tokens=10,
            completion_tokens=10,
            source=chain,
        )
        assert result == (0.0, "unpriced")
        assert catalog.fetch_calls == 0

    def test_warm_catalog_serves_without_fetch(self) -> None:
        catalog = _FetchingCatalog({"long/tail-model": _META}, warm=True)
        chain = ChainedModelMetadataResolver(static=_StaticOnly({}), openrouter=catalog)
        _, basis = compute_turn_cost(
            provider="openrouter",
            model="long/tail-model",
            prompt_tokens=10,
            completion_tokens=10,
            source=chain,
        )
        assert basis == "estimate_catalog"
        assert catalog.fetch_calls == 0

    def test_fetch_only_resolver_is_skipped_on_the_turn_path(self) -> None:
        # A resolver that cannot promise fetch-free lookup is skipped under
        # allow_fetch=False — honest unpriced beats a maybe-fetch.
        catalog = _FetchOnlyCatalog()
        chain = ChainedModelMetadataResolver(static=_StaticOnly({}), openrouter=catalog)
        result = compute_turn_cost(
            provider="openrouter",
            model="whatever/model",
            prompt_tokens=10,
            completion_tokens=10,
            source=chain,
        )
        assert result == (0.0, "unpriced")
        assert catalog.fetch_calls == 0

    def test_router_path_allow_fetch_true_uses_resolve(self) -> None:
        # resolve_with_source(allow_fetch=True) — the non-turn arm — may fetch.
        catalog = _FetchingCatalog({"long/tail-model": _META}, warm=False)
        chain = ChainedModelMetadataResolver(static=_StaticOnly({}), openrouter=catalog)
        hit = chain.resolve_with_source("long/tail-model", allow_fetch=True)
        assert hit is not None
        assert hit[1] == "catalog"
        assert catalog.fetch_calls == 1


#: Every (provider, model) key the deleted ``_PRICE_TABLE`` served — post-M2
#: they MUST all still resolve through the static chain (coverage parity, F4;
#: NB with the AUTHORITATIVE Spec-23 numbers, not the deleted placeholders).
_LEGACY_PRICE_TABLE_KEYS = [
    ("anthropic", "claude-sonnet-4-6"),
    ("anthropic", "claude-haiku-4-5"),
    ("deepseek", "deepseek-chat"),
    ("groq", "llama-3.1-8b-instant"),
    ("nvidia", "llama-3.3-nemotron-super-49b-v1.5"),
    ("nvidia", "nemotron-3-super-120b-a12b"),
    ("nvidia", "nemotron-3-nano-omni-30b-a3b-reasoning"),
]


class TestLegacyTableParity:
    """No coverage regression from deleting ``_PRICE_TABLE`` (M2-T1, F4)."""

    @pytest.mark.parametrize(("provider", "model"), _LEGACY_PRICE_TABLE_KEYS)
    def test_legacy_key_resolves_static_with_nonzero_cost(self, provider: str, model: str) -> None:
        # source=None → the real zero-network static-only default chain.
        cents, basis = compute_turn_cost(
            provider=provider,
            model=model,
            prompt_tokens=1000,
            completion_tokens=1000,
        )
        assert basis == "estimate_static"
        assert cents > 0.0

    def test_prefixed_nvidia_still_resolves(self) -> None:
        # The Spec-25 §2.6 lesson: prefixed catalog ids must not silently miss.
        cents, basis = compute_turn_cost(
            provider="nvidia",
            model="nvidia/llama-3.3-nemotron-super-49b-v1.5",
            prompt_tokens=1000,
            completion_tokens=1000,
        )
        assert basis == "estimate_static"
        assert cents == pytest.approx(0.30 + 0.60)  # authoritative nvidia row

    def test_anthropic_authoritative_price(self) -> None:
        # $3 / $15 per Mtok (live-verified 2026-07-12) — 1000/500 tokens.
        cents, _ = compute_turn_cost(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=1000,
            completion_tokens=500,
        )
        assert cents == pytest.approx(0.30 + 1.50 * 0.5)

    def test_haiku_4_5_does_not_carry_the_haiku_3_5_placeholder(self) -> None:
        # The deleted table priced haiku-4-5 at Haiku 3.5's rate (0.08/0.40);
        # the authoritative row is $1/$5 per Mtok → 0.10/0.50 cents per 1k.
        cents, _ = compute_turn_cost(
            provider="anthropic",
            model="claude-haiku-4-5",
            prompt_tokens=1000,
            completion_tokens=1000,
        )
        assert cents == pytest.approx(0.10 + 0.50)

    def test_zero_tokens_zero_cost_still_static_basis(self) -> None:
        cents, basis = compute_turn_cost(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=0,
            completion_tokens=0,
        )
        assert (cents, basis) == (0.0, "estimate_static")
