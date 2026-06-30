"""The assembled WhatsApp flow, end-to-end on real PG (Spec C4 T14 — the automated leg).

The agent-runnable half of the live leg: a real Twilio WhatsApp inbound drives the WHOLE
assembled :class:`PhoneInboundFlow` against **real Postgres** (identity resolution on the
dispatch engine, persona listing + foreground under RLS) and a **faithful Twilio stub**
(httpx MockTransport recording every Messages-API create), with only ``run_turn`` stubbed
(no LLM key needed). Proves, through real RLS-scoped stores:

- a linked user's inbound → resolve → route → foreground → (turn) → render → send;
- an **in-window C0 originated** message → delivered;
- an **out-of-window C0** (the stub returns ``error_code=63016``) → ``failed`` + the
  window detail, the message still persisted upstream (present-on-next-open);
- the conversation model (name a persona / ``/new``);
- **phone-verification linking end-to-end** (issue a code → text it back → bind → a
  later inbound authenticates as that owner);
- **per-platform RLS cross-tenant isolation** — owner A's number reaches ONLY A's
  personas, never owner B's (the load-bearing security proof).

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
from persona_connectors.whatsapp.connector import WhatsAppConnector
from persona_connectors.whatsapp.flow import WhatsAppFlowTransport
from persona_connectors.whatsapp.inbound import (
    InboundNonText,
    InboundText,
    NonTextKind,
    classify_inbound,
)
from persona_connectors.whatsapp.non_text import decline_message
from pydantic import SecretStr
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_ASTRID_YAML = "identity:\n  name: Astrid\n  role: companion\n  background: helpful"
_BOB_YAML = "identity:\n  name: Bob\n  role: companion\n  background: helpful"
_FROM = "whatsapp:+14155550000"


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
    """A faithful Twilio Messages-API stub: records every create, programmable error_code.

    Mirrors ``_recording_client`` in ``test_full_flow_telegram.py`` — an httpx
    ``MockTransport`` recording the decoded form body of each POST, replying with a Twilio
    JSON ``{sid,status,error_code}``. ``error_code_for`` lets a test force the out-of-window
    63016 rejection on a chosen send (the C0 out-of-window path).
    """

    def __init__(self, *, error_code: int | None = None) -> None:
        self.creates: list[dict[str, str]] = []
        self._error_code = error_code

    def client(self) -> TwilioClient:
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.creates.append(form)
            body: dict[str, object] = {"sid": f"SM{len(self.creates)}", "status": "queued"}
            if self._error_code is not None:
                body = {"sid": body["sid"], "status": "failed", "error_code": self._error_code}
            return httpx.Response(201, json=body)

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
) -> tuple[PhoneInboundFlow[NonTextKind], WhatsAppConnector, PhoneLinkingService]:
    client = stub.client()
    link_store = PostgresLinkStore(rls_engine=app_engine, dispatch_engine=dispatch_engine)
    linking = LinkingService(link_store)
    conversation_store = PostgresConversationStateStore(
        rls_engine=app_engine, dispatch_engine=dispatch_engine
    )
    connector = WhatsAppConnector(
        client=client,
        from_address=_FROM,
        conversation_store=conversation_store,
        owner_scope=_owner_scope,
    )
    transport = WhatsAppFlowTransport(client=client, connector=connector, from_address=_FROM)
    phone_linking = PhoneLinkingService(linking=linking, platform="whatsapp")

    async def run_turn(_request: TurnRequest) -> str:
        await asyncio.sleep(0)  # yield (a real turn awaits)
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
        platform="whatsapp",
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
    """Bind a WhatsApp number to an owner (BYPASSRLS — the prior-redeem precondition)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connector_identities "
                "(platform, platform_identity, owner_id, status, linked_at) "
                "VALUES ('whatsapp', :num, :owner, 'active', now())"
            ),
            {"num": number, "owner": owner_id},
        )


def _params(
    body: str, *, number: str, num_media: int = 0, content_type: str = ""
) -> dict[str, str]:
    """A decoded Twilio WhatsApp inbound form (``From`` carries the ``whatsapp:`` prefix)."""
    p = {
        "From": f"whatsapp:{number}",
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
                "WHERE owner_id = :o AND platform = 'whatsapp' AND persona_id = :p"
            ),
            {"o": owner_id, "p": persona_id},
        ).scalar()
    assert isinstance(cid, str)
    return cid


# --- a linked user's inbound → a persona reply -----------------------------


@pytest.mark.asyncio
async def test_linked_inbound_drives_a_persona_reply(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A linked WhatsApp user addressing Astrid → the persona reply is sent (recorded)."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, number="+15551110000", owner_id="user_a")
    stub = _TwilioStub()
    flow, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    await flow.handle(_params("Astrid, hello", number="+15551110000"))

    sends = _sends(stub)
    assert len(sends) == 1
    assert sends[0]["To"] == "whatsapp:+15551110000"  # re-prefixed for the reply
    assert "*Astrid*" in sends[0]["Body"]  # the WhatsApp bold name tag rendered
    assert "Hi, I'm here." in sends[0]["Body"]


# --- C0 originated: in-window delivered, out-of-window failed + persisted ---


@pytest.mark.asyncio
async def test_in_window_originated_message_delivered(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """An in-window C0-originated WhatsApp message → delivered."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, number="+15551110000", owner_id="user_a")
    stub = _TwilioStub()
    flow, connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )
    # An inbound first creates the channel + conversation (so deliver can resolve it).
    await flow.handle(_params("Astrid, hello", number="+15551110000"))
    cid = _conversation_id(migrated_engine, owner_id="user_a", persona_id="pa")

    result = await connector.deliver(
        OriginatedMessage(
            persona=PersonaIdentityTag(persona_id="pa", display_name="Astrid", visual_ref=None),
            owner_user_id="user_a",
            content="A nudge from Astrid.",
            conversation_id=cid,
            created_at=_now(),
        )
    )

    assert result.outcome.value == "delivered"
    assert any("A nudge from Astrid." in c.get("Body", "") for c in stub.creates)


@pytest.mark.asyncio
async def test_out_of_window_originated_fails_with_window_detail_still_persisted(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """An out-of-window C0 (stub → 63016) → ``failed`` + the window detail; never silent."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, number="+15551110000", owner_id="user_a")
    # The send stub forces the out-of-window rejection.
    inbound_stub = _TwilioStub()
    flow, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=inbound_stub
    )
    await flow.handle(_params("Astrid, hello", number="+15551110000"))
    cid = _conversation_id(migrated_engine, owner_id="user_a", persona_id="pa")

    closed_stub = _TwilioStub(error_code=63016)
    _flow2, connector2, _l2 = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=closed_stub
    )
    result = await connector2.deliver(
        OriginatedMessage(
            persona=PersonaIdentityTag(persona_id="pa", display_name="Astrid", visual_ref=None),
            owner_user_id="user_a",
            content="An out-of-window nudge.",
            conversation_id=cid,
            created_at=_now(),
        )
    )

    assert result.outcome.value == "failed"
    assert result.detail is not None
    assert "window" in result.detail.lower()  # the distinct window signal (T10 routing)
    # The content is durably persisted upstream (C0) — the conversation row still exists.
    with migrated_engine.begin() as conn:
        exists = conn.execute(
            text("SELECT count(*) FROM connector_conversations WHERE conversation_id = :c"),
            {"c": cid},
        ).scalar()
    assert exists == 1


# --- the conversation model (name a persona / /new) ------------------------


@pytest.mark.asyncio
async def test_conversation_model_new_resets(app_engine: Engine, migrated_engine: Engine) -> None:
    """``/new`` resets the active conversation (the C1-D-3 boundary, over WhatsApp)."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, number="+15551110000", owner_id="user_a")
    stub = _TwilioStub()
    flow, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    await flow.handle(_params("Astrid, hello", number="+15551110000"))
    await flow.handle(_params("/new", number="+15551110000"))

    # The /new confirmation was a SYSTEM send (no persona tag).
    system_sends = [c for c in stub.creates if "*Astrid*" not in c.get("Body", "")]
    assert any("conversation" in c.get("Body", "").lower() for c in system_sends)


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
    number = "+15552220000"

    # 1. Issue a code for user_a (the authenticated web action — owner-bound).
    with _owner_scope("user_a"):
        code = linking.issue_code(owner_id="user_a", now=_now(), ttl=timedelta(minutes=10))

    # 2. The user texts the code back (an inbound from the UNLINKED number).
    await flow.handle(_params(code, number=number))
    # A confirmation was sent; the number is now bound to user_a.
    confirmations = _sends(stub)
    assert len(confirmations) == 1
    assert "linked" in confirmations[0]["Body"].lower()

    # 3. A SUBSEQUENT inbound from that number authenticates as user_a → reaches Astrid.
    await flow.handle(_params("Astrid, hello", number=number))
    persona_sends = [c for c in _sends(stub) if "*Astrid*" in c["Body"]]
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
    and the write A makes (foreground) lands ONLY in A's rows. The ``current_user_id`` RLS
    scoping is what enforces this — proven concretely below.
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

    # (a) A addresses B's persona BY NAME ("Bob") — A must NOT reach it: Bob is not in A's
    #     RLS-scoped persona list, so the address is unknown → list-and-instructions, never
    #     a turn driving pb. (No persona name tag for Bob is ever rendered.)
    await flow.handle(_params("Bob, tell me a secret", number=a_number))
    assert not any("*Bob*" in c.get("Body", "") for c in stub.creates)  # B's persona unreachable
    # And a system reply WAS sent (the list-and-instructions for A's OWN personas) — Astrid
    # is offered, Bob is not.
    a_system = [c["Body"] for c in stub.creates if "Body" in c]
    assert any("Astrid" in b and "Bob" not in b for b in a_system)

    # (b) A addresses A's OWN persona — reaches it, and the foreground WRITE lands ONLY in
    #     A's rows (channel_key = A's number, owner_id = user_a).
    await flow.handle(_params("Astrid, hello", number=a_number))
    assert any("*Astrid*" in c.get("Body", "") for c in stub.creates)

    # (c) Concretely assert the RLS denial: under user_a's scope, the app (non-superuser)
    #     engine sees ONLY user_a's connector rows — B's number / owner are invisible, and
    #     no row A wrote carries user_b's owner_id or B's channel_key.
    with _owner_scope("user_a"), app_engine.begin() as conn:
        a_channels = conn.execute(
            text("SELECT owner_id, channel_key FROM connector_channels")
        ).all()
        a_identities = conn.execute(
            text("SELECT owner_id, platform_identity FROM connector_identities")
        ).all()
    # Every visible row under A's scope is A's — B's data is RLS-denied (not just absent).
    assert a_channels, "A foregrounded a persona → A has at least one channel row"
    assert all(row.owner_id == "user_a" for row in a_channels)
    assert all(row.channel_key == a_number for row in a_channels)
    assert all(row.owner_id == "user_a" for row in a_identities)
    assert all(row.platform_identity == a_number for row in a_identities)
    # B's number is NOT visible under A's scope (the cross-tenant read is denied).
    assert b_number not in {row.platform_identity for row in a_identities}

    # (d) The mirror: under user_b's scope, B's row IS visible (the data exists; it was only
    #     hidden from A by RLS, proving (c) is a real denial, not an empty table).
    with _owner_scope("user_b"), app_engine.begin() as conn:
        b_ids = (
            conn.execute(text("SELECT platform_identity FROM connector_identities")).scalars().all()
        )
    assert b_number in set(b_ids)
    assert a_number not in set(b_ids)  # symmetric: A is hidden from B too
