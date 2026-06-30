"""The assembled SMS flow, end-to-end on real PG (Spec C4 T14 — the automated leg).

The agent-runnable half of the live leg: a real Twilio SMS inbound drives the WHOLE
assembled :class:`PhoneInboundFlow` against **real Postgres** (identity resolution on the
dispatch engine, persona listing + foreground under RLS) and a **faithful Twilio stub**
(httpx MockTransport recording every Messages-API create), with only ``run_turn`` stubbed.
Proves, through real RLS-scoped stores:

- a linked user's inbound → reply (segmented if long, but Twilio auto-concatenates → ONE
  create), capped to a single send (the T12 segment budget);
- a **C0 originated SMS sends outside any window** (criterion 5 — no window gating, unlike
  WhatsApp);
- a long reply is capped to ONE send (the segment cap bounds runaway cost);
- **phone-verification linking end-to-end** (issue → text back → bind → authenticate);
- **per-platform RLS cross-tenant isolation** — A's number reaches ONLY A's personas.

The real-Twilio round-trip (a live number + a signed webhook) is the user-run operator
pass; this is the CI-automatable proof.
"""

from __future__ import annotations

import asyncio
import contextlib
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona_api.middleware.rls_context import current_user_id
from persona_connectors._phone.flow import PhoneInboundFlow
from persona_connectors._phone.linking import PhoneLinkingService
from persona_connectors._twilio.client import TwilioClient
from persona_connectors.composition import build_persona_name_lister
from persona_connectors.domain.flow import SharedInboundFlow, TurnRequest
from persona_connectors.domain.linking import LinkingService
from persona_connectors.domain.resolution import InboundIdentityResolver
from persona_connectors.infra import PostgresConversationStateStore, PostgresLinkStore
from persona_connectors.sms.connector import SmsConnector
from persona_connectors.sms.flow import SmsFlowTransport
from persona_connectors.sms.inbound import (
    InboundNonText,
    InboundText,
    NonTextKind,
    classify_inbound,
)
from persona_connectors.sms.non_text import decline_message
from pydantic import SecretStr
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_ASTRID_YAML = "identity:\n  name: Astrid\n  role: companion\n  background: helpful"
_BOB_YAML = "identity:\n  name: Bob\n  role: companion\n  background: helpful"
_FROM = "+14155550000"  # bare E.164 — no whatsapp: prefix for SMS


@contextlib.contextmanager
def _owner_scope(owner_id: str) -> Iterator[None]:
    token = current_user_id.set(owner_id)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _now() -> datetime:
    return datetime.now(UTC)


class _TwilioStub:
    """A faithful Twilio Messages-API stub: records every create (httpx MockTransport)."""

    def __init__(self) -> None:
        self.creates: list[dict[str, str]] = []

    def client(self) -> TwilioClient:
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.creates.append(form)
            return httpx.Response(201, json={"sid": f"SM{len(self.creates)}", "status": "queued"})

        transport = httpx.MockTransport(handler)
        return TwilioClient(
            account_sid="ACxxxx",
            auth_token=SecretStr("token"),
            http=httpx.AsyncClient(transport=transport),
        )


def _assemble(
    *,
    app_engine: Engine,
    dispatch_engine: Engine,
    stub: _TwilioStub,
    reply: str = "Hi, I'm here.",
) -> tuple[PhoneInboundFlow[NonTextKind], SmsConnector, PhoneLinkingService]:
    client = stub.client()
    link_store = PostgresLinkStore(rls_engine=app_engine, dispatch_engine=dispatch_engine)
    linking = LinkingService(link_store)
    conversation_store = PostgresConversationStateStore(
        rls_engine=app_engine, dispatch_engine=dispatch_engine
    )
    connector = SmsConnector(
        client=client,
        from_address=_FROM,
        conversation_store=conversation_store,
        owner_scope=_owner_scope,
    )
    transport = SmsFlowTransport(client=client, connector=connector, from_address=_FROM)
    phone_linking = PhoneLinkingService(linking=linking, platform="sms")

    async def run_turn(_request: TurnRequest) -> str:
        await asyncio.sleep(0)
        return reply

    shared = SharedInboundFlow(
        resolver=InboundIdentityResolver(linking),
        conversation_store=conversation_store,
        list_persona_names=build_persona_name_lister(
            rls_engine=app_engine, owner_scope=_owner_scope
        ),
        run_turn=run_turn,
    )
    flow: PhoneInboundFlow[NonTextKind] = PhoneInboundFlow(
        platform="sms",
        classify_inbound=classify_inbound,
        inbound_text_type=InboundText,
        inbound_non_text_type=InboundNonText,
        decline_message=decline_message,
        linking=phone_linking,
        shared=shared,
        transport=transport,
        now=_now,
    )
    return flow, connector, phone_linking


def _seed_persona(engine: Engine, *, persona_id: str, yaml: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE personas SET yaml = :y WHERE id = :pid"), {"y": yaml, "pid": persona_id}
        )


def _link(engine: Engine, *, number: str, owner_id: str) -> None:
    """Bind an SMS number to an owner (BYPASSRLS — the prior-redeem precondition)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connector_identities "
                "(platform, platform_identity, owner_id, status, linked_at) "
                "VALUES ('sms', :num, :owner, 'active', now())"
            ),
            {"num": number, "owner": owner_id},
        )


def _params(
    body: str, *, number: str, num_media: int = 0, content_type: str = ""
) -> dict[str, str]:
    """A decoded Twilio SMS inbound form (``From`` is bare E.164 — no prefix)."""
    p = {
        "From": number,
        "To": _FROM,
        "MessageSid": f"SMin{abs(hash((body, number))) % 10_000}",
        "Body": body,
        "NumMedia": str(num_media),
    }
    if content_type:
        p["MediaContentType0"] = content_type
    return p


def _sends(stub: _TwilioStub) -> list[dict[str, str]]:
    return [c for c in stub.creates if "Body" in c]


def _conversation_id(engine: Engine, *, owner_id: str, persona_id: str) -> str:
    with _owner_scope(owner_id), engine.begin() as conn:
        cid = conn.execute(
            text(
                "SELECT conversation_id FROM connector_conversations "
                "WHERE owner_id = :o AND platform = 'sms' AND persona_id = :p"
            ),
            {"o": owner_id, "p": persona_id},
        ).scalar()
    assert isinstance(cid, str)
    return cid


# --- a linked user's inbound → a reply -------------------------------------


@pytest.mark.asyncio
async def test_linked_inbound_drives_a_persona_reply(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A linked SMS user addressing Astrid → the reply is sent (recorded), to a bare number."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, number="+15551110000", owner_id="user_a")
    stub = _TwilioStub()
    flow, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    await flow.handle(_params("Astrid, hello", number="+15551110000"))

    sends = _sends(stub)
    assert len(sends) == 1
    assert sends[0]["To"] == "+15551110000"  # bare E.164, no prefix
    assert "Astrid" in sends[0]["Body"]  # the plain-prefix name tag
    assert "Hi, I'm here." in sends[0]["Body"]


@pytest.mark.asyncio
async def test_long_reply_is_capped_to_one_send(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A long reply is rendered to ONE Twilio create (the segment cap bounds the cost)."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, number="+15551110000", owner_id="user_a")
    stub = _TwilioStub()
    long_reply = "word " * 400  # ~2000 chars, far past one GSM-7 segment
    flow, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub, reply=long_reply
    )

    await flow.handle(_params("Astrid, hello", number="+15551110000"))

    # ONE create — Twilio auto-concatenates the billed segments; the cap bounds the body.
    assert len(_sends(stub)) == 1


# --- C0 originated SMS sends outside any window (criterion 5) ---------------


@pytest.mark.asyncio
async def test_originated_sms_sends_with_no_window_gate(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A C0-originated SMS sends freely — there is NO window gate (the WhatsApp asymmetry)."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, number="+15551110000", owner_id="user_a")
    stub = _TwilioStub()
    flow, connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )
    await flow.handle(_params("Astrid, hello", number="+15551110000"))
    cid = _conversation_id(migrated_engine, owner_id="user_a", persona_id="pa")

    # No inbound just before this — there is no "window" concept for SMS; it still sends.
    result = await connector.deliver(
        OriginatedMessage(
            persona=PersonaIdentityTag(persona_id="pa", display_name="Astrid", visual_ref=None),
            owner_user_id="user_a",
            content="A spontaneous check-in.",
            conversation_id=cid,
            created_at=_now(),
        )
    )

    assert result.outcome.value == "delivered"  # no window gating denied it
    assert any("A spontaneous check-in." in c.get("Body", "") for c in stub.creates)


# --- phone-verification linking, end-to-end --------------------------------


@pytest.mark.asyncio
async def test_phone_verification_linking_end_to_end(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """Issue a code → the user texts it back → it binds → a later inbound authenticates."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    stub = _TwilioStub()
    flow, _connector, linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )
    number = "+15553330000"

    with _owner_scope("user_a"):
        code = linking.issue_code(owner_id="user_a", now=_now(), ttl=timedelta(minutes=10))

    await flow.handle(_params(code, number=number))
    confirmations = _sends(stub)
    assert len(confirmations) == 1
    assert "linked" in confirmations[0]["Body"].lower()

    await flow.handle(_params("Astrid, hello", number=number))
    persona_sends = [
        c for c in _sends(stub) if "Astrid" in c["Body"] and "linked" not in c["Body"].lower()
    ]
    assert len(persona_sends) == 1  # the now-linked number reached its owner's persona


# --- ⚠️ PER-PLATFORM RLS CROSS-TENANT ISOLATION (the load-bearing test) -----


@pytest.mark.asyncio
async def test_cross_tenant_isolation_a_cannot_reach_b(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """Owner A's number resolves to A and reaches ONLY A's personas — never owner B's.

    Owner A links one number; owner B links a DIFFERENT number. An inbound from A's
    number must resolve to user_a, reach A's persona (pa/Astrid), and be DENIED any read
    or write of B's data: B's persona (pb/Bob) is neither listed nor addressable for A,
    and A's foreground write lands ONLY in A's rows. ``current_user_id`` RLS scoping is the
    enforcement — proven concretely below.
    """
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _seed_persona(migrated_engine, persona_id="pb", yaml=_BOB_YAML)
    a_number = "+15551110000"
    b_number = "+15559990000"
    _link(migrated_engine, number=a_number, owner_id="user_a")
    _link(migrated_engine, number=b_number, owner_id="user_b")

    stub = _TwilioStub()
    flow, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    # (a) A addresses B's persona by name ("Bob") — Bob is NOT in A's RLS-scoped list, so
    #     the address is unknown → list-and-instructions, never a turn driving pb (no
    #     ``Bob:`` persona-reply prefix is ever rendered for A).
    await flow.handle(_params("Bob, tell me a secret", number=a_number))
    assert not any(c.get("Body", "").startswith("Bob:") for c in stub.creates)
    a_system = [c["Body"] for c in stub.creates if "Body" in c]
    assert any("Astrid" in b and "Bob" not in b for b in a_system)  # A's own personas only

    # (b) A addresses A's OWN persona — reaches it; the write lands ONLY in A's rows.
    await flow.handle(_params("Astrid, hello", number=a_number))
    assert any(c.get("Body", "").startswith("Astrid:") for c in stub.creates)

    # (c) Concretely assert the RLS denial: under user_a's scope, the app (non-superuser)
    #     engine sees ONLY user_a's connector rows — B's number / owner are invisible.
    with _owner_scope("user_a"), app_engine.begin() as conn:
        a_channels = conn.execute(
            text("SELECT owner_id, channel_key FROM connector_channels")
        ).all()
        a_identities = conn.execute(
            text("SELECT owner_id, platform_identity FROM connector_identities")
        ).all()
    assert a_channels, "A foregrounded a persona → A has at least one channel row"
    assert all(row.owner_id == "user_a" for row in a_channels)
    assert all(row.channel_key == a_number for row in a_channels)
    assert all(row.owner_id == "user_a" for row in a_identities)
    assert b_number not in {row.platform_identity for row in a_identities}  # B is RLS-denied

    # (d) The mirror: under user_b's scope, B's row IS visible (the data exists; RLS only
    #     hid it from A) — proving (c) is a real denial, not an empty table.
    with _owner_scope("user_b"), app_engine.begin() as conn:
        b_ids = (
            conn.execute(text("SELECT platform_identity FROM connector_identities")).scalars().all()
        )
    assert b_number in set(b_ids)
    assert a_number not in set(b_ids)
