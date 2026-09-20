"""One process holds ONE origination service and ONE channel registry (R9-081, R9-120, T3).

The 2026-09-21 ruling let the connector deliver its own. Read literally in a process that
hosts BOTH halves behind ``PERSONA_API_EMBED_CONNECTORS``, that builds a second
``OriginationService`` beside the api's: two services, two channel sets, and whichever one
happened to run would decide where a persona speaks. The coordinator clarified the ruling
to rule that out, so these are the assertions that make the clarification enforceable.

Three properties, and the third is the one that would otherwise rot silently:

1. **Exactly one** ``OriginationService`` is constructed per boot, with the flag on.
2. **One registry**, the api's, is the one the connector composition binds into.
3. **It is always bound**, in every branch: flag off, flag on with nothing configured, and
   flag on with a connector that failed to start. "This process has no channels" has to be
   a statement, never the absence of one, or an unbound registry reads as an empty one and
   origination quietly goes back to web only.

Every construction is counted at the SOURCE module, because both the api lifespan and the
connector composition import these names at call time; patching a host module's attribute
would miss one of the two callers, which is exactly the blindness being tested for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition
from persona_api.services import task_origination_composition
from persona_api.services.origination_delivery import ChannelDeliverers

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI

_PLATFORM_API_RESPONSES = {
    "getMe": {"ok": True, "result": {"username": "opbot", "id": "1"}},
    "users/@me": {"id": "999", "username": "opbot"},
    "auth.test": {"ok": True, "user_id": "U1", "team_id": "T1"},
}


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    for marker, payload in _PLATFORM_API_RESPONSES.items():
        if marker in url:
            return httpx.Response(200, json=payload)
    return httpx.Response(200, json={"ok": True, "result": {"username": "opbot", "id": "1"}})


def _config(tmp_path: Path, *, embed: bool) -> APIConfig:
    return APIConfig(
        edition=Edition.community,
        embed_connectors=embed,
        community_db_path=str(tmp_path / "community.db"),
        community_memory_path=str(tmp_path / "memory"),
        audit_root=str(tmp_path / "audit"),
        workspace_root=str(tmp_path / "workspace"),
    )


@pytest.fixture
def telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured Telegram bot, and no real network for its identity probe."""
    monkeypatch.setenv("PERSONA_CONNECTORS_TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("PERSONA_CONNECTORS_EDITION", "community")
    monkeypatch.setenv("PERSONA_CONNECTORS_JWT_SECRET", "test-jwt-secret")
    # Capture the real class BEFORE patching: a lambda that calls ``httpx.AsyncClient``
    # would resolve to itself and recurse, and the embedded host swallows the resulting
    # error as a connector failure, which reads as a clean "no connectors" boot.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_k: real_client(transport=httpx.MockTransport(_handler)),
    )


@pytest.fixture
def origination_services_built(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Count every ``compose_task_origination_services`` result, from its OWN module."""
    built: list[object] = []
    real = task_origination_composition.compose_task_origination_services

    def _counting(**kwargs: Any) -> Any:  # noqa: ANN401 - a passthrough of the real signature
        services = real(**kwargs)
        built.append(services.origination)
        return services

    monkeypatch.setattr(
        task_origination_composition, "compose_task_origination_services", _counting
    )
    return built


def _boot(config: APIConfig) -> Iterator[FastAPI]:
    app = create_app(config)
    with TestClient(app):
        yield app


def test_with_connectors_embedded_the_process_builds_exactly_one_origination_service(
    tmp_path: Path,
    telegram_env: None,  # noqa: ARG001 - fixture applied for its env + transport patch
    origination_services_built: list[object],
) -> None:
    """THE T3 assertion: one process, one service deciding where a persona speaks.

    A second one is not a duplicate object, it is a second answer. The api's notifiers
    would hold one registry and the connector's another, and which one a given failure
    account went through would depend on which code path raised it.
    """
    for app in _boot(_config(tmp_path, embed=True)):
        assert app.state.embedded_connectors is not None, (
            "the flag is on and Telegram is configured; without a hosted connector this "
            "test would pass for the wrong reason"
        )

    assert len(origination_services_built) == 1, (
        f"{len(origination_services_built)} origination services in one process"
    )


def test_the_connector_binds_into_the_api_s_own_registry(
    tmp_path: Path,
    telegram_env: None,  # noqa: ARG001 - fixture applied for its env + transport patch
) -> None:
    """One registry, and it is the one the api's notifiers already hold.

    Asserting the CONTENT rather than merely ``bound`` is what makes this fail if the
    connector composition were handed a fresh registry: the api's would still be bound,
    by ``ensure_bound``, and it would be empty.
    """
    for app in _boot(_config(tmp_path, embed=True)):
        channels: ChannelDeliverers = app.state.channel_deliverers
        embedded = app.state.embedded_connectors

        assert channels.bound is True
        assert sorted(channels.snapshot()) == ["telegram"]
        assert sorted(channels.snapshot()) == sorted(embedded.platforms)


def test_with_the_flag_off_the_registry_is_bound_and_empty(tmp_path: Path) -> None:
    """Web only, declared. An unbound registry would look exactly the same from outside."""
    for app in _boot(_config(tmp_path, embed=False)):
        channels: ChannelDeliverers = app.state.channel_deliverers

        assert channels.bound is True
        assert channels.snapshot() == {}


def test_with_the_flag_on_but_nothing_configured_the_registry_is_still_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-I1-6: the api serves on, and still says what it can reach.

    This is the branch that returns ``None`` from ``start_embedded_connectors`` without
    ever entering the connector composition, so nothing else could have bound.
    """
    monkeypatch.delenv("PERSONA_CONNECTORS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("PERSONA_CONNECTORS_EDITION", "community")

    for app in _boot(_config(tmp_path, embed=True)):
        assert app.state.embedded_connectors is None
        assert app.state.channel_deliverers.bound is True
        assert app.state.channel_deliverers.snapshot() == {}


def test_a_connector_that_fails_to_start_still_leaves_a_bound_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure branch, which is the one an ordering guarantee is most likely to miss.

    Composition reaches the live platforms, so a revoked token or an outage raises there.
    The api keeps serving (D-I1-6), and it must still be able to say where a persona can
    speak, or a transient platform outage at boot would silently disable origination
    routing for the whole process lifetime.
    """
    monkeypatch.setenv("PERSONA_CONNECTORS_TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("PERSONA_CONNECTORS_EDITION", "community")

    from persona_connectors import service as connector_service

    async def _explode(**_kwargs: object) -> object:
        msg = "telegram getMe: 401 unauthorized"
        raise RuntimeError(msg)

    monkeypatch.setattr(connector_service, "build_connectors", _explode)

    for app in _boot(_config(tmp_path, embed=True)):
        assert app.state.embedded_connectors is None
        assert app.state.channel_deliverers.bound is True
        assert app.state.channel_deliverers.snapshot() == {}


def test_every_worker_side_notifier_forwards_the_registry_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task digest must reach the same channels a chat reply does (R9-081 blast radius).

    The A4 digest sender, the budget-extend ask, the stuck account and the approval
    announce are composed in ``worker_root``, a THIRD root beside the api's chat path and
    the connector root. Wiring two and forgetting the third is the R9-212 / R9-213 /
    R9-217 shape: one behaviour, several composition sites, a fix that reached some.

    This drives the four REAL hook builders and asserts identity, not presence: every
    notifier must hold the very object it was handed. An equivalent-but-different
    registry fails, because that is precisely the drift being ruled out. The other half,
    that the api's worker entry actually passes one, is the source guard below: a
    forwarding proof alone stays compatible with an entry that never forwards.
    """
    from persona_api.background import worker_root
    from persona_api.services import origination_adapters

    seen: list[object] = []
    for name in (
        "OriginatorUpdateSender",
        "OriginatorFailureNotifier",
        "OriginatorApprovalNotifier",
    ):
        real = getattr(origination_adapters, name)

        def _recording(*args: Any, _real: Any = real, **kwargs: Any) -> Any:  # noqa: ANN401
            seen.append(kwargs.get("channels"))
            return _real(*args, **kwargs)

        monkeypatch.setattr(origination_adapters, name, _recording)

    sentinel = ChannelDeliverers()
    sentinel.bind({})
    common: dict[str, Any] = {
        "rls_engine": MagicMock(),
        "memory_backend": MagicMock(),
        "edition": Edition.community,
        "audit_root": tmp_path / "audit",
        "audit_logger": None,
        "channels": sentinel,
    }

    worker_root._build_milestone_hook(live_sessions=None, **common)  # noqa: SLF001
    worker_root._build_budget_gate(  # noqa: SLF001
        task_store=MagicMock(),
        live_sessions=None,
        emit_task_updated=lambda *_a: None,
        **common,
    )
    worker_root._build_task_stuck_hook(  # noqa: SLF001
        task_store=MagicMock(), live_sessions=None, **common
    )
    worker_root._build_approval_announce_hook(**common)  # noqa: SLF001

    assert len(seen) >= 4, f"only {len(seen)} worker-side notifiers were composed"
    assert all(holder is sentinel for holder in seen), (
        "a worker-side notifier holds a different channel registry from the one it was "
        "given, so a persona would report on its work somewhere the user is not"
    )


def test_the_api_actually_hands_its_registry_to_the_worker(tmp_path: Path) -> None:  # noqa: ARG001
    """The half a forwarding test cannot prove: the real entry passes one.

    ``start_in_process_worker`` refuses a non-Postgres engine, so a community boot never
    reaches it and no behavioural test here can. That is the R9-081 trap exactly: the
    harness above would stay green for ever while the api passed nothing. So the call
    site is read, the way A10-D-9 reads one.
    """
    import pathlib as _pathlib

    from persona_api import app as app_module

    source = _pathlib.Path(app_module.__file__).read_text(encoding="utf-8")
    after_call = source.split("in_process_worker = start_in_process_worker(")[1]
    call = after_call.split("\n            )")[0]

    assert "channels=channel_deliverers," in call, (
        "the in-process worker is composed without the process's channel registry, so "
        "task digests and approval asks would never reach a connector"
    )


def test_a_second_registry_cannot_be_smuggled_into_the_embedded_host(
    tmp_path: Path,
    telegram_env: None,  # noqa: ARG001 - fixture applied for its env + transport patch
) -> None:
    """``build_connectors`` binds what it is GIVEN, so the host's registry is the only one.

    The counterpart to the one-service assertion: one service is worthless if it reads a
    registry nobody bound the connectors into.
    """
    from persona_connectors import service as connector_service

    captured: list[ChannelDeliverers] = []
    real = connector_service.build_connectors

    async def _capturing(**kwargs: Any) -> Any:  # noqa: ANN401 - passthrough
        captured.append(kwargs["channels"])
        return await real(**kwargs)

    app = create_app(_config(tmp_path, embed=True))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(connector_service, "build_connectors", _capturing)
        with TestClient(app):
            pass

    assert len(captured) == 1, captured
    assert captured[0] is app.state.channel_deliverers


def test_the_lifespan_declares_its_channels_on_the_one_path_every_boot_takes(
    tmp_path: Path,  # noqa: ARG001 - kept for signature symmetry with its siblings
) -> None:
    """The ordering guarantee, read off the real source rather than trusted.

    ``ensure_bound`` is what makes "no channels" a declaration. If it ever moved inside a
    branch, the branches that skipped it would hand every notifier an unbound registry
    that behaves exactly like an empty one, and no behavioural test would go red. Same
    idiom as the connector root's binding guard, for the same reason.
    """
    import pathlib

    from persona_api import app as app_module

    source = pathlib.Path(app_module.__file__).read_text(encoding="utf-8")
    body = source.split("async def _lifespan")[1].split("\ndef ")[0]

    assert body.count("channel_deliverers.ensure_bound()") == 1
    assert "\n    channel_deliverers.ensure_bound()\n" in body, (
        "ensure_bound is indented into a branch, so some boots leave the registry unbound"
    )
    assert body.index("channel_deliverers.ensure_bound()") < body.index("yield"), (
        "the channels must be declared before the app serves a request"
    )
