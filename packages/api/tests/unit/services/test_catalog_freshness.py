"""M2-T6 — the api lifespan catalog-freshness task + the /v1/models refresh hook.

Unit-level (fakes, no network):

* ``refresh_catalog_once``: reindex follows a refresh; a cold resolver gets
  its boot-time index build even when the client was already fresh (the F5
  warm); a fresh client + warm resolver is a no-op; ``resolver=None`` safe.
* ``run_catalog_refresh_loop``: TTL off → warm once and EXIT; TTL on → warm
  immediately, keep polling, cancel cleanly.
* the ``/v1/models`` service hook: the default-client path refreshes-if-stale
  and reindexes the paired resolver; an INJECTED client is never asked to
  (a fake without ``refresh_if_stale`` must not crash the service).
"""

from __future__ import annotations

import asyncio

import pytest
from persona_api.services import model_catalog_service
from persona_api.services.catalog_freshness import (
    refresh_catalog_once,
    run_catalog_refresh_loop,
)


class _FakeClient:
    def __init__(self, *, ttl_s: float = 100.0, refresh_result: bool = False) -> None:
        self.ttl_s = ttl_s
        self._refresh_result = refresh_result
        self.refresh_calls = 0

    def refresh_if_stale(self) -> bool:
        self.refresh_calls += 1
        return self._refresh_result


class _FakeResolver:
    def __init__(self, *, warm: bool) -> None:
        self.warm = warm
        self.reindex_calls = 0

    def reindex(self) -> None:
        self.reindex_calls += 1
        self.warm = True


class TestRefreshOnce:
    def test_reindex_follows_a_refresh(self) -> None:
        client = _FakeClient(refresh_result=True)
        resolver = _FakeResolver(warm=True)
        assert refresh_catalog_once(client, resolver) is True  # type: ignore[arg-type]
        assert resolver.reindex_calls == 1

    def test_cold_resolver_gets_the_boot_index_even_without_a_refresh(self) -> None:
        # F5: another consumer may have warmed the client cache first — the
        # derived index must still be built so the turn path can serve.
        client = _FakeClient(refresh_result=False)
        resolver = _FakeResolver(warm=False)
        refresh_catalog_once(client, resolver)  # type: ignore[arg-type]
        assert resolver.reindex_calls == 1

    def test_fresh_client_warm_resolver_is_a_no_op(self) -> None:
        client = _FakeClient(refresh_result=False)
        resolver = _FakeResolver(warm=True)
        refresh_catalog_once(client, resolver)  # type: ignore[arg-type]
        assert resolver.reindex_calls == 0

    def test_resolver_none_is_safe(self) -> None:
        assert refresh_catalog_once(_FakeClient(refresh_result=True), None) is True  # type: ignore[arg-type]


class TestRefreshLoop:
    @pytest.mark.asyncio
    async def test_ttl_off_warms_once_and_exits(self) -> None:
        client = _FakeClient(ttl_s=0.0, refresh_result=True)
        resolver = _FakeResolver(warm=False)
        # Completes on its own — no cancellation needed.
        await asyncio.wait_for(
            run_catalog_refresh_loop(client, resolver),  # type: ignore[arg-type]
            timeout=5.0,
        )
        assert client.refresh_calls == 1
        assert resolver.reindex_calls == 1

    @pytest.mark.asyncio
    async def test_ttl_on_warms_immediately_and_cancels_cleanly(self) -> None:
        client = _FakeClient(ttl_s=3600.0, refresh_result=True)
        resolver = _FakeResolver(warm=False)
        task = asyncio.create_task(
            run_catalog_refresh_loop(client, resolver)  # type: ignore[arg-type]
        )
        # Let the immediate warm step run (it is the first await in the task).
        for _ in range(50):
            if client.refresh_calls:
                break
            await asyncio.sleep(0.01)
        assert client.refresh_calls == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_warm_failure_is_swallowed(self) -> None:
        class _ExplodingClient:
            ttl_s = 0.0

            def refresh_if_stale(self) -> bool:
                raise RuntimeError("boom")

        # Must not raise — the warm is best-effort (fail-soft forever).
        await asyncio.wait_for(
            run_catalog_refresh_loop(_ExplodingClient(), None),  # type: ignore[arg-type]
            timeout=5.0,
        )


class _HookClient:
    """A default-client stand-in for the service hook test."""

    def __init__(self, *, refresh_result: bool) -> None:
        self._refresh_result = refresh_result
        self.refresh_calls = 0

    def refresh_if_stale(self) -> bool:
        self.refresh_calls += 1
        return self._refresh_result

    def list_models(self, *, force_refresh: bool = False) -> tuple[object, ...]:  # noqa: ARG002
        return ()


class _InjectedClientWithoutRefresh:
    """An injected test client — the service must NEVER call refresh on it."""

    def list_models(self, *, force_refresh: bool = False) -> tuple[object, ...]:  # noqa: ARG002
        return ()


class TestServiceHook:
    def test_default_path_refreshes_and_reindexes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _HookClient(refresh_result=True)
        resolver = _FakeResolver(warm=True)
        monkeypatch.setattr(model_catalog_service, "_default_client", lambda: client)
        monkeypatch.setattr(model_catalog_service, "_default_resolver", lambda: resolver)

        result = model_catalog_service.list_models()

        assert client.refresh_calls == 1
        assert resolver.reindex_calls == 1
        assert result.source == "openrouter"

    def test_default_path_fresh_catalog_skips_reindex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _HookClient(refresh_result=False)
        resolver = _FakeResolver(warm=True)
        monkeypatch.setattr(model_catalog_service, "_default_client", lambda: client)
        monkeypatch.setattr(model_catalog_service, "_default_resolver", lambda: resolver)

        model_catalog_service.list_models()

        assert client.refresh_calls == 1  # the staleness CHECK always runs
        assert resolver.reindex_calls == 0  # nothing changed → no rebuild

    def test_injected_client_is_never_asked_to_refresh(self) -> None:
        # The fake has NO refresh_if_stale — if the service called it, this
        # would raise AttributeError. Scripted test fixtures stay untouched.
        result = model_catalog_service.list_models(
            client=_InjectedClientWithoutRefresh()  # type: ignore[arg-type]
        )
        assert result.source == "openrouter"
