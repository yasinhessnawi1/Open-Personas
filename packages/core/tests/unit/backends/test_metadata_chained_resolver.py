"""Unit tests for Spec 23 T5 ChainedModelMetadataResolver (D-23-X-resolver-precedence)."""

from __future__ import annotations

from persona.backends.metadata.chained_resolver import ChainedModelMetadataResolver
from persona.backends.model_metadata import ModelMetadata, ModelMetadataResolver


class _MapResolver:
    """Tiny in-memory resolver for composition tests."""

    def __init__(self, table: dict[str, ModelMetadata]) -> None:
        self._table = table

    def resolve(self, model_id: str) -> ModelMetadata | None:
        return self._table.get(model_id)


def _md(quality: float, *, cost: float = 0.1) -> ModelMetadata:
    return ModelMetadata(
        cost_input_per_1k_tokens=cost,
        cost_output_per_1k_tokens=cost,
        latency_p50_ms=200.0,
        quality_benchmark=quality,
        tools_supported=True,
        vision_supported=False,
        context_length=100_000,
    )


class TestChainedResolverPrecedence:
    def test_satisfies_protocol(self) -> None:
        resolver = ChainedModelMetadataResolver(static=_MapResolver({}), openrouter=None)
        assert isinstance(resolver, ModelMetadataResolver)

    def test_static_wins_when_present(self) -> None:
        # D-23-X-resolver-precedence: curated (static) record is authoritative,
        # even when the model is ALSO in OpenRouter.
        static = _MapResolver({"a/b": _md(0.9)})
        openrouter = _MapResolver({"a/b": _md(0.5)})  # neutral catalog quality
        resolver = ChainedModelMetadataResolver(static=static, openrouter=openrouter)
        hit = resolver.resolve("a/b")
        assert hit is not None
        assert hit.quality_benchmark == 0.9  # static authored quality, not 0.5

    def test_openrouter_serves_coverage_when_static_misses(self) -> None:
        static = _MapResolver({})
        openrouter = _MapResolver({"long/tail": _md(0.5)})
        resolver = ChainedModelMetadataResolver(static=static, openrouter=openrouter)
        hit = resolver.resolve("long/tail")
        assert hit is not None
        assert hit.quality_benchmark == 0.5

    def test_both_miss_returns_none(self) -> None:
        resolver = ChainedModelMetadataResolver(
            static=_MapResolver({}), openrouter=_MapResolver({})
        )
        assert resolver.resolve("nope/none") is None

    def test_static_only_chain(self) -> None:
        resolver = ChainedModelMetadataResolver(
            static=_MapResolver({"a/b": _md(0.8)}), openrouter=None
        )
        assert resolver.resolve("a/b") is not None
        assert resolver.resolve("x/y") is None

    def test_openrouter_only_chain(self) -> None:
        resolver = ChainedModelMetadataResolver(
            static=None, openrouter=_MapResolver({"a/b": _md(0.5)})
        )
        assert resolver.resolve("a/b") is not None
        assert resolver.resolve("x/y") is None

    def test_empty_chain_returns_none(self) -> None:
        assert ChainedModelMetadataResolver(static=None, openrouter=None).resolve("a/b") is None


class _NoFetchMapResolver(_MapResolver):
    """A catalog-link fake exposing the M2 ``resolve_no_fetch`` arm.

    ``resolve`` counts as a fetch (a cold real resolver would hit the network
    there); ``resolve_no_fetch`` serves only when ``warm``.
    """

    def __init__(self, table: dict[str, ModelMetadata], *, warm: bool) -> None:
        super().__init__(table)
        self.warm = warm
        self.fetch_calls = 0

    def resolve(self, model_id: str) -> ModelMetadata | None:
        self.fetch_calls += 1
        return super().resolve(model_id)

    def resolve_no_fetch(self, model_id: str) -> ModelMetadata | None:
        return super().resolve(model_id) if self.warm else None


class TestResolveWithSource:
    """Spec M2 (D-M2-1): provenance + the no-fetch turn-path arm."""

    def test_static_hit_reports_static(self) -> None:
        resolver = ChainedModelMetadataResolver(
            static=_MapResolver({"a/b": _md(0.9)}), openrouter=_MapResolver({"a/b": _md(0.5)})
        )
        hit = resolver.resolve_with_source("a/b")
        assert hit is not None
        metadata, source = hit
        assert source == "static"
        assert metadata.quality_benchmark == 0.9  # precedence preserved

    def test_catalog_hit_reports_catalog(self) -> None:
        resolver = ChainedModelMetadataResolver(
            static=_MapResolver({}), openrouter=_MapResolver({"long/tail": _md(0.5)})
        )
        hit = resolver.resolve_with_source("long/tail")
        assert hit is not None
        assert hit[1] == "catalog"

    def test_both_miss_returns_none(self) -> None:
        resolver = ChainedModelMetadataResolver(
            static=_MapResolver({}), openrouter=_MapResolver({})
        )
        assert resolver.resolve_with_source("nope/none") is None

    def test_no_fetch_uses_the_no_fetch_arm(self) -> None:
        catalog = _NoFetchMapResolver({"long/tail": _md(0.5)}, warm=True)
        resolver = ChainedModelMetadataResolver(static=_MapResolver({}), openrouter=catalog)
        hit = resolver.resolve_with_source("long/tail", allow_fetch=False)
        assert hit is not None
        assert hit[1] == "catalog"
        assert catalog.fetch_calls == 0  # never went near resolve()

    def test_no_fetch_cold_catalog_is_a_miss(self) -> None:
        catalog = _NoFetchMapResolver({"long/tail": _md(0.5)}, warm=False)
        resolver = ChainedModelMetadataResolver(static=_MapResolver({}), openrouter=catalog)
        assert resolver.resolve_with_source("long/tail", allow_fetch=False) is None
        assert catalog.fetch_calls == 0

    def test_no_fetch_skips_a_resolver_without_the_arm(self) -> None:
        # A composed resolver that cannot promise fetch-free lookup is skipped
        # under allow_fetch=False (honest miss beats a maybe-fetch, M2 §2).
        resolver = ChainedModelMetadataResolver(
            static=_MapResolver({}), openrouter=_MapResolver({"long/tail": _md(0.5)})
        )
        assert resolver.resolve_with_source("long/tail", allow_fetch=False) is None

    def test_allow_fetch_true_uses_resolve(self) -> None:
        catalog = _NoFetchMapResolver({"long/tail": _md(0.5)}, warm=False)
        resolver = ChainedModelMetadataResolver(static=_MapResolver({}), openrouter=catalog)
        hit = resolver.resolve_with_source("long/tail", allow_fetch=True)
        assert hit is not None
        assert catalog.fetch_calls == 1  # the router arm may fetch
