"""M2-T6 — catalog TTL mechanics on OpenRouterCatalogClient (D-M2-6).

Pins, with a counting MockTransport and a controllable clock:

* staleness semantics (never-fetched = stale; TTL window; ``<= 0`` = off),
* ``refresh_if_stale`` as the ONLY fetch trigger (``list_models`` NEVER
  auto-refetches on staleness — the zero-fetch turn invariant's client half),
* stale-serve on refresh failure (old entries keep serving; no raise),
* ``catalog_ttl_from_env`` parsing (default / explicit / off / malformed),
* the resolver's ``reindex`` following a client refresh WITHOUT a second
  fetch, and the ``warm`` flag driving the boot-time index build.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from persona.backends.metadata.openrouter_resolver import OpenRouterModelMetadataResolver
from persona.backends.openrouter_catalog import (
    DEFAULT_CATALOG_TTL_S,
    OpenRouterCatalogClient,
    catalog_ttl_from_env,
)

if TYPE_CHECKING:
    import pytest


def _entry(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "context_length": 128000,
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": ["tools"],
    }


class _Clock:
    """A controllable stand-in for the module's ``time`` binding."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Counter:
    """Mutable request counter shared with the transport handler."""

    def __init__(self) -> None:
        self.requests = 0


def _client(
    *,
    ttl_s: float,
    payloads: list[dict[str, Any] | int],
) -> tuple[OpenRouterCatalogClient, _Counter]:
    """A client over a scripted transport; each request pops the next payload.

    A ``dict`` payload answers 200 with it; an ``int`` answers that status.
    The last payload repeats once the script is exhausted.
    """
    counter = _Counter()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        counter.requests += 1
        idx = min(counter.requests - 1, len(payloads) - 1)
        scripted = payloads[idx]
        if isinstance(scripted, int):
            return httpx.Response(scripted, json={})
        return httpx.Response(200, json=scripted)

    client = OpenRouterCatalogClient(
        "sk-or-test",
        transport=httpx.MockTransport(handler),
        ttl_s=ttl_s,
    )
    return client, counter


def _catalog(*ids: str) -> dict[str, Any]:
    return {"data": [_entry(i) for i in ids]}


class TestStalenessSemantics:
    def test_never_fetched_is_stale(self) -> None:
        client, _ = _client(ttl_s=100.0, payloads=[_catalog("a/b")])
        assert client.is_stale is True

    def test_refresh_if_stale_fetches_once_then_fresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        monkeypatch.setattr("persona.backends.openrouter_catalog.time", clock)
        client, counter = _client(ttl_s=100.0, payloads=[_catalog("a/b")])
        assert client.refresh_if_stale() is True  # the warm arm
        assert counter.requests == 1
        assert client.is_stale is False
        assert client.refresh_if_stale() is False  # fresh → fast no-op
        assert counter.requests == 1

    def test_ttl_expiry_marks_stale_and_refresh_refetches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        monkeypatch.setattr("persona.backends.openrouter_catalog.time", clock)
        client, counter = _client(ttl_s=100.0, payloads=[_catalog("a/b"), _catalog("c/d")])
        client.refresh_if_stale()
        clock.now += 101.0
        assert client.is_stale is True
        assert client.refresh_if_stale() is True
        assert counter.requests == 2
        assert [e.id for e in client.list_models()] == ["c/d"]

    def test_ttl_off_never_stale_after_first_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        monkeypatch.setattr("persona.backends.openrouter_catalog.time", clock)
        client, counter = _client(ttl_s=0.0, payloads=[_catalog("a/b")])
        assert client.refresh_if_stale() is True  # the warm still fetches once
        clock.now += 1_000_000.0
        assert client.is_stale is False  # process-lifetime semantics (D-22-5)
        assert client.refresh_if_stale() is False
        assert counter.requests == 1

    def test_list_models_never_auto_refetches_when_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The zero-fetch invariant's client half: staleness is advisory;
        # ONLY refresh_if_stale acts on it.
        clock = _Clock()
        monkeypatch.setattr("persona.backends.openrouter_catalog.time", clock)
        client, counter = _client(ttl_s=50.0, payloads=[_catalog("a/b"), _catalog("c/d")])
        client.refresh_if_stale()
        clock.now += 999.0  # far past the TTL
        assert client.is_stale is True
        assert [e.id for e in client.list_models()] == ["a/b"]  # stale copy serves
        assert counter.requests == 1  # NO auto-refetch


class TestStaleServeOnFailure:
    def test_failed_refresh_keeps_serving_the_stale_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        monkeypatch.setattr("persona.backends.openrouter_catalog.time", clock)
        client, counter = _client(ttl_s=50.0, payloads=[_catalog("a/b"), 500])
        client.refresh_if_stale()
        clock.now += 51.0
        assert client.refresh_if_stale() is False  # failed — no raise
        assert counter.requests == 2  # the attempt happened
        assert [e.id for e in client.list_models()] == ["a/b"]  # stale-serve
        assert client.is_stale is True  # still due; the next check retries


class TestTtlFromEnv:
    def test_unset_is_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_OPENROUTER_CATALOG_TTL_S", raising=False)
        assert catalog_ttl_from_env() == DEFAULT_CATALOG_TTL_S

    def test_explicit_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_OPENROUTER_CATALOG_TTL_S", "3600")
        assert catalog_ttl_from_env() == 3600.0

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_OPENROUTER_CATALOG_TTL_S", "0")
        assert catalog_ttl_from_env() == 0.0

    def test_malformed_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_OPENROUTER_CATALOG_TTL_S", "six-hours")
        assert catalog_ttl_from_env() == DEFAULT_CATALOG_TTL_S


class TestResolverReindexAndWarm:
    def test_reindex_follows_a_client_refresh_without_a_second_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        monkeypatch.setattr("persona.backends.openrouter_catalog.time", clock)
        client, counter = _client(
            ttl_s=50.0, payloads=[_catalog("old/model"), _catalog("new/model")]
        )
        resolver = OpenRouterModelMetadataResolver(client)
        assert resolver.warm is False
        assert resolver.resolve("old/model") is not None  # builds index (fetch 1)
        assert resolver.warm is True
        assert counter.requests == 1

        clock.now += 51.0
        assert client.refresh_if_stale() is True  # fetch 2 → cache now "new/model"
        # The derived index is still the OLD one until reindex...
        assert resolver.resolve_no_fetch("new/model") is None
        resolver.reindex()  # ...and reindex rebuilds WITHOUT fetch 3.
        assert resolver.resolve_no_fetch("new/model") is not None
        assert resolver.resolve_no_fetch("old/model") is None
        assert counter.requests == 2
