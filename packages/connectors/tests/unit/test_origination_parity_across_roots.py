"""The api and the connector originate through ONE seam, proven by their records (R9-120, T4).

This is the condition the 2026-09-21 ruling was granted under. The owner chose that the
connector delivers its own; the dissent attached to that choice was that a second delivery
path is a second thing to keep honest, and the mitigation was made part of the work: one
seam both callers enter, no second copy of the resolve-and-send logic, and a test that an
origination delivered by a connector produces the same audit record shape as one delivered
by the api. If the two paths CAN drift, the task is not done.

The assertion is deliberately made on the OUTPUT, not on the wiring. Wiring assertions live
in ``test_connector_origination_wiring.py`` and prove the paths are COMPOSED the same. These
prove they RECORD the same, which is the property that survives someone later changing one
of them without reading the other.

Both roots are driven for real: the api's through ``compose_task_origination_services``, the
function ``app.py`` calls, and the connector's by running ``build_connectors`` and taking the
services it actually composed, rather than composing a look-alike beside it.

The ledger half of the same condition is
``packages/api/tests/integration/test_origination_ledger_parity.py``, marked ``integration``:
credits need Postgres, and integration does not run locally by owner rule, so that half's
evidence comes from CI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from persona.schema.origination import PersonaIdentityTag
from persona_api.approvals.cadence import MessagePriority
from persona_api.approvals.failure import FailureAccount, FailureKind
from persona_api.config import APIConfig, Edition
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import connector_conversations, conversations, personas
from persona_api.services.origination_delivery import ChannelDeliverers
from persona_api.services.task_origination_composition import (
    TaskOriginationServices,
    compose_task_origination_services,
)
from persona_connectors import service
from persona_connectors.config import ConnectorConfig
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "p1"
_API_CONVERSATION = "conv_raised_by_api"
_CONNECTOR_CONVERSATION = "conv_raised_by_connector"
_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


#: Every Telegram ``sendMessage`` the real adapter makes, as ``(chat_id, text)``.
_SENDS: list[tuple[str, str]] = []


def _handler(request: httpx.Request) -> httpx.Response:
    """Stand in for Telegram: answer the setup probe, record what gets sent.

    The deliverer under test is the REAL ``TelegramConnector`` the connector root
    composed, so the only thing faked here is the wire. A recording stand-in deliverer
    would have skipped the GAP-A conversation lookup, which is the half of resolve-and-send
    most likely to differ between two roots.
    """
    url = str(request.url)
    if "getMe" in url:
        return httpx.Response(200, json={"ok": True, "result": {"username": "opbot", "id": "1"}})
    if "sendMessage" in url:
        payload = json.loads(request.content or b"{}")
        _SENDS.append((str(payload.get("chat_id", "")), str(payload.get("text", ""))))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(_SENDS)}})
    return httpx.Response(200, json={"ok": True, "result": {}})


@pytest.fixture(autouse=True)
def _clear_sends() -> None:
    _SENDS.clear()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    """The shared outbound client, alive for the whole test.

    The deliverer under test is the real adapter, so it sends on this client LONG after
    composition returns. Closing it at the end of the build (the obvious shape) makes
    every delivery raise "client has been closed" inside a best-effort path.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        yield client


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """One community database holding two conversations, both living on Telegram."""
    eng = make_community_engine(tmp_path / "parity.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="alice@example.com")
    with eng.begin() as conn:
        conn.execute(
            insert(personas).values(id=_PERSONA, owner_id=_OWNER, yaml="identity:\n  name: Ada\n")
        )
        for index, conversation_id in enumerate((_API_CONVERSATION, _CONNECTOR_CONVERSATION)):
            conn.execute(
                insert(conversations).values(
                    id=conversation_id, owner_id=_OWNER, persona_id=_PERSONA, title="t"
                )
            )
            conn.execute(
                insert(connector_conversations).values(
                    id=f"cc{index}",
                    owner_id=_OWNER,
                    platform="telegram",
                    channel_key=f"5551234{index}",
                    persona_id=_PERSONA,
                    conversation_id=conversation_id,
                    status="active",
                )
            )
    return eng


def _api_config(tmp_path: Path) -> APIConfig:
    return APIConfig(
        edition=Edition.community,
        community_db_path=str(tmp_path / "community.db"),
        community_memory_path=str(tmp_path / "memory"),
        audit_root=str(tmp_path / "audit"),
        workspace_root=str(tmp_path / "workspace"),
    )


def _account() -> FailureAccount:
    """The same account both roots will voice, so only the PATH can differ."""
    return FailureAccount(
        kind=FailureKind.ORIGINATION_FAILED,
        task_id="task_1",
        headline="I could not set that up after all.",
        cause="the schedule was rejected",
        options=("try again", "pick another time"),
        priority=MessagePriority.FAILURE,
    )


def _tag() -> PersonaIdentityTag:
    return PersonaIdentityTag(persona_id=_PERSONA, display_name="Ada")


def _compose_as_the_api_does(
    engine: Engine, tmp_path: Path, channels: ChannelDeliverers
) -> TaskOriginationServices:
    """Root A: the exact call ``app.py``'s lifespan makes."""
    return compose_task_origination_services(
        rls_engine=engine,
        memory_backend=MagicMock(),
        edition=Edition.community,
        audit_root=tmp_path / "audit",
        live_sessions=None,
        channels=channels,
    )


async def _compose_as_the_connector_does(
    engine: Engine, tmp_path: Path, http: httpx.AsyncClient
) -> tuple[TaskOriginationServices, Any]:
    """Root B: whatever the REAL ``build_connectors`` composed, captured on its way out.

    Not a reconstruction. If the connector root stops composing these, or composes them
    differently, this returns that rather than a tidy stand-in that agrees.

    Returns the services AND the real ``TelegramConnector`` it bound, so the api root can
    be given the same adapter and the comparison is genuinely of the two PATHS rather than
    of two different deliverers.
    """
    captured: list[TaskOriginationServices] = []
    real = service.compose_task_origination_services

    def _capturing(**kwargs: Any) -> TaskOriginationServices:  # noqa: ANN401 - passthrough
        services = real(**kwargs)
        captured.append(services)
        return services

    runtime_factory = MagicMock()
    runtime_factory.memory_backend = MagicMock()
    connector_config = ConnectorConfig(
        edition="community",
        jwt_secret="test-jwt-secret",  # noqa: S106 - test literal
        telegram_bot_token="tg-token",  # noqa: S106 - test literal
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service, "compose_task_origination_services", _capturing)
        bundle = await service.build_connectors(
            connector_config=connector_config,
            api_config=_api_config(tmp_path),
            rls_engine=engine,
            dispatch_engine=engine,
            runtime_factory=runtime_factory,
            credits_policy=MagicMock(),
            job_queue=MagicMock(),
            stripe_gateway=None,
            http=http,
        )

    assert len(captured) == 1, "the connector root did not compose its origination services"
    assert bundle.channels.bound is True
    assert sorted(bundle.channels.snapshot()) == ["telegram"]
    return captured[0], bundle.channels.snapshot()["telegram"]


def _routing_rows(engine: Engine, conversation_id: str) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(audit_log_t.c.action, audit_log_t.c.user_id, audit_log_t.c.metadata).where(
                    audit_log_t.c.target == conversation_id
                )
            )
            .mappings()
            .all()
        ]


@pytest.mark.asyncio
async def test_both_roots_record_the_same_audit_shape_for_the_same_origination(
    engine: Engine, tmp_path: Path, http: httpx.AsyncClient
) -> None:
    """THE condition the ruling was granted under, read off the durable record.

    Same account, same persona, same owner, same platform, same adapter. The only
    difference is which composition root raised it. Every audit row the two produce must
    agree in action, in metadata keys and in the routed channel, because they are
    supposed to be the same code holding equivalent registries.
    """
    connector_services, telegram = await _compose_as_the_connector_does(engine, tmp_path, http)
    api_channels = ChannelDeliverers()
    api_channels.bind({"telegram": telegram})
    api_services = _compose_as_the_api_does(engine, tmp_path, api_channels)

    await api_services.origination._notifier.notify(  # noqa: SLF001 - the seam under test
        _account(), persona=_tag(), owner_id=_OWNER, conversation_id=_API_CONVERSATION
    )
    await connector_services.origination._notifier.notify(  # noqa: SLF001
        _account(), persona=_tag(), owner_id=_OWNER, conversation_id=_CONNECTOR_CONVERSATION
    )

    by_api = _routing_rows(engine, _API_CONVERSATION)
    by_connector = _routing_rows(engine, _CONNECTOR_CONVERSATION)

    # Non-empty FIRST: every comparison below is vacuously true over two empty lists.
    assert by_api, "the api root wrote no audit row at all"
    assert by_connector, "the connector root wrote no audit row at all"

    assert sorted(r["action"] for r in by_api) == sorted(r["action"] for r in by_connector)
    assert {k for r in by_api for k in r["metadata"]} == {
        k for r in by_connector for k in r["metadata"]
    }

    api_routing = next(r for r in by_api if r["action"] == "origination.routing")
    connector_routing = next(r for r in by_connector if r["action"] == "origination.routing")
    assert api_routing["metadata"] == connector_routing["metadata"], (
        "the two roots recorded different routing decisions for the same situation"
    )
    assert api_routing["metadata"]["channel"] == "telegram"
    assert api_routing["metadata"]["outcome"] == "delivered"
    assert api_routing["user_id"] == connector_routing["user_id"] == _OWNER


@pytest.mark.asyncio
async def test_both_roots_reach_telegram_with_the_same_words(
    engine: Engine, tmp_path: Path, http: httpx.AsyncClient
) -> None:
    """The behaviour behind the record: each account reaches the chat, not a web tab.

    Paired with the audit assertion on purpose. Two paths can write identical audit rows
    and still put different things on the wire, so the record is checked AND the send is.
    Reading it off the transport also exercises the GAP-A conversation lookup, which a
    stand-in deliverer would have skipped.
    """
    connector_services, telegram = await _compose_as_the_connector_does(engine, tmp_path, http)
    api_channels = ChannelDeliverers()
    api_channels.bind({"telegram": telegram})
    api_services = _compose_as_the_api_does(engine, tmp_path, api_channels)

    await api_services.origination._notifier.notify(  # noqa: SLF001
        _account(), persona=_tag(), owner_id=_OWNER, conversation_id=_API_CONVERSATION
    )
    await connector_services.origination._notifier.notify(  # noqa: SLF001
        _account(), persona=_tag(), owner_id=_OWNER, conversation_id=_CONNECTOR_CONVERSATION
    )

    assert len(_SENDS) == 2, f"expected one Telegram send per root, got {_SENDS}"
    (api_chat, api_text), (connector_chat, connector_text) = _SENDS
    # Each went to the chat ITS conversation is bound to, not to a shared default.
    assert api_chat == "55512340"
    assert connector_chat == "55512341"
    # Byte-identical user-visible text: the render is shared too, so a persona does not
    # sound like a different product depending on which root raised the account.
    assert api_text == connector_text
    assert "I could not set that up after all." in api_text


@pytest.mark.asyncio
async def test_the_two_roots_run_the_same_resolve_and_send_code(
    engine: Engine, tmp_path: Path, http: httpx.AsyncClient
) -> None:
    """No second copy of the resolve-and-send logic, counted rather than asserted about.

    Counting entries into the ONE factory both roots must use is what makes "one seam"
    falsifiable. A root that grew its own router would still deliver and still audit; it
    would simply stop coming through this function, and only a count sees that.
    """
    from persona_api.services import origination_adapters, origination_delivery

    connector_services, telegram = await _compose_as_the_connector_does(engine, tmp_path, http)
    api_channels = ChannelDeliverers()
    api_channels.bind({"telegram": telegram})
    api_services = _compose_as_the_api_does(engine, tmp_path, api_channels)

    entered: list[object] = []
    real = origination_delivery.build_origination_router

    def _counting(**kwargs: Any) -> Any:  # noqa: ANN401 - passthrough
        entered.append(kwargs.get("channels"))
        return real(**kwargs)

    with pytest.MonkeyPatch.context() as patch:
        # Patched where the caller RESOLVES it. ``origination_adapters`` imported the name
        # at module load, so patching only the source module would count nothing and pass.
        patch.setattr(origination_adapters, "build_origination_router", _counting)
        await api_services.origination._notifier.notify(  # noqa: SLF001
            _account(), persona=_tag(), owner_id=_OWNER, conversation_id=_API_CONVERSATION
        )
        await connector_services.origination._notifier.notify(  # noqa: SLF001
            _account(), persona=_tag(), owner_id=_OWNER, conversation_id=_CONNECTOR_CONVERSATION
        )

    assert len(entered) == 2, (
        f"{len(entered)} of the 2 originations went through the shared router factory; a "
        "root that built its own is exactly the drift this condition rules out"
    )
    assert all(holder is not None for holder in entered), (
        "an origination was routed with no channel registry, so it could only reach the web"
    )
