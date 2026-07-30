"""The assembled email flow, end-to-end on real PG (Spec C5, Group E — the automated leg).

Drives the WHOLE assembled email connector through the **mounted ASGI webhook** (a real
Postmark-shaped POST via ``TestClient``), against **real Postgres** (identity resolution on
the dispatch engine, persona listing + foreground under RLS) and a **faithful Postmark stub**
(httpx MockTransport recording every send), with only ``run_turn`` stubbed. This is the
A4-lesson test: the real wired path — B1 (Basic-Auth) → parse → B2 (DMARC verdict) → shared
flow → threaded reply — proven reachable through the actual app + real composition, not a
hand-instantiated harness. Proves:

- **B1 fail rejects before parse** — a forged/missing-auth POST → 401, no send;
- a **B1+B2-authentic** linked user → a threaded persona reply (``Re:`` + ``In-Reply-To``);
- **B2 fail (spoofed From, dmarc≠pass) → zero access** — no bind, no reply;
- a **C0 originated email sends with no window** (the freest channel — criterion 7);
- **email-verification linking end-to-end** (issue via the authed route → email the code back
  → bind → authenticate);
- **per-platform RLS cross-tenant isolation** — A's address reaches ONLY A's personas.

The real-Postmark round-trip (real Basic-Auth webhook + real Authentication-Results + real
send/deliverability) is the R4 user-run operator pass; this is the CI-automatable proof.

**R9-072:** Postmark's inbound-parse payload never actually carries an
``Authentication-Results`` header — its documented ``Headers`` array only ever has
``X-Spam-Tests`` (SpamAssassin) and ``Received-SPF``. ``test_real_postmark_shape_...`` below
drives the mounted app with that real shape (no ``Authentication-Results`` at all, only
``X-Spam-Tests: ...,DKIM_VALID_AU,...``) to prove B2 is reachable on real Postmark traffic,
not just on the synthetic ``Authentication-Results`` shape the rest of this file still uses
(kept because a future/non-Postmark source stamping that header must still work).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from persona.auth.jwt_verifier import AuthenticatedUser
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona_api.middleware.rls_context import current_user_id
from persona_connectors._postmark.client import PostmarkClient
from persona_connectors._postmark.webhook import PostmarkWebhookAuth
from persona_connectors.composition import build_persona_name_lister
from persona_connectors.domain.flow import SharedInboundFlow
from persona_connectors.domain.linking import LinkingService
from persona_connectors.domain.resolution import InboundIdentityResolver
from persona_connectors.email.app import build_email_app
from persona_connectors.email.connector import EmailConnector
from persona_connectors.email.flow import EmailInboundFlow
from persona_connectors.email.linking import EmailLinkingService
from persona_connectors.infra import PostgresConversationStateStore, PostgresLinkStore
from pydantic import SecretStr
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_ASTRID_YAML = "identity:\n  name: Astrid\n  role: companion\n  background: helpful"
_BOB_YAML = "identity:\n  name: Bob\n  role: companion\n  background: helpful"
_FROM_ADDRESS = "inbound@personas.app"
_AUTH = PostmarkWebhookAuth(username="hook", password=SecretStr("s3cret"))
_BASIC = "Basic " + base64.b64encode(b"hook:s3cret").decode()
_DMARC_PASS = "mx.postmark.com; dkim=pass; spf=pass; dmarc=pass (p=REJECT)"
_DMARC_FAIL = "mx.postmark.com; dkim=fail; spf=softfail; dmarc=fail (p=REJECT)"


@contextlib.contextmanager
def _owner_scope(owner_id: str) -> Iterator[None]:
    token = current_user_id.set(owner_id)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _now() -> datetime:
    return datetime.now(UTC)


class _PostmarkStub:
    """A faithful Postmark send stub: records every send body (httpx MockTransport)."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []

    def client(self) -> PostmarkClient:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            self.sends.append(json.loads(request.content))
            return httpx.Response(200, json={"MessageID": f"pm-{len(self.sends)}", "ErrorCode": 0})

        transport = httpx.MockTransport(handler)
        return PostmarkClient(
            server_token=SecretStr("tok"), http=httpx.AsyncClient(transport=transport)
        )


def _assemble(
    *,
    app_engine: Engine,
    dispatch_engine: Engine,
    stub: _PostmarkStub,
    reply: str = "Hi, I'm here.",
) -> tuple[FastAPI, EmailConnector, EmailLinkingService]:
    client = stub.client()
    linking = LinkingService(
        PostgresLinkStore(rls_engine=app_engine, dispatch_engine=dispatch_engine)
    )
    conversation_store = PostgresConversationStateStore(
        rls_engine=app_engine, dispatch_engine=dispatch_engine
    )

    def recipient_for(owner_id: str) -> str | None:
        with _owner_scope(owner_id), app_engine.begin() as conn:
            return conn.execute(
                text(
                    "SELECT platform_identity FROM connector_identities "
                    "WHERE owner_id = :o AND platform = 'email' AND status = 'active' LIMIT 1"
                ),
                {"o": owner_id},
            ).scalar()

    connector = EmailConnector(
        client=client, from_address=_FROM_ADDRESS, recipient_for=recipient_for
    )
    email_linking = EmailLinkingService(linking=linking)

    async def run_turn(_request: object) -> str:
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
    email_flow = EmailInboundFlow(
        connector=connector, linking=email_linking, shared=shared, now=_now
    )

    async def issue_code(owner_id: str) -> str:
        with _owner_scope(owner_id):
            return email_linking.issue_code(
                owner_id=owner_id, now=_now(), ttl=timedelta(minutes=10)
            )

    async def verify_jwt(bearer: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=bearer, email=None)

    app = build_email_app(
        webhook_auth=_AUTH,
        on_inbound=email_flow.handle,
        issue_code=issue_code,
        verify_jwt=verify_jwt,
        now=_now,
    )
    return app, connector, email_linking


def _seed_persona(engine: Engine, *, persona_id: str, yaml: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE personas SET yaml = :y WHERE id = :pid"), {"y": yaml, "pid": persona_id}
        )


def _link(engine: Engine, *, address: str, owner_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connector_identities "
                "(platform, platform_identity, owner_id, status, linked_at) "
                "VALUES ('email', :addr, :owner, 'active', now())"
            ),
            {"addr": address, "owner": owner_id},
        )


def _payload(
    *,
    text_body: str,
    sender: str,
    mailbox_hash: str = "astrid",
    subject: str = "Re: Deposit dispute",
    message_id: str = "<in-1@mail.example.com>",
    references: str | None = "<root@mail.example.com>",
    auth_results: str | None = _DMARC_PASS,
    spam_tests: str | None = None,
) -> dict[str, Any]:
    headers = [{"Name": "Message-ID", "Value": message_id}]
    if auth_results is not None:
        headers.append({"Name": "Authentication-Results", "Value": auth_results})
    if spam_tests is not None:
        headers.append({"Name": "X-Spam-Tests", "Value": spam_tests})
    if references:
        headers.append({"Name": "References", "Value": references})
        headers.append({"Name": "In-Reply-To", "Value": references})
    return {
        "From": sender,
        "FromFull": {"Email": sender, "Name": "Bob"},
        "To": f"inbound+{mailbox_hash}@personas.app" if mailbox_hash else "inbound@personas.app",
        "MailboxHash": mailbox_hash,
        "Subject": subject,
        "MessageID": "postmark-uuid",
        "StrippedTextReply": text_body,
        "TextBody": text_body,
        "Attachments": [],
        "Headers": headers,
    }


def _post(app: FastAPI, payload: dict[str, Any], *, auth: str | None = _BASIC) -> httpx.Response:
    headers = {"Authorization": auth} if auth is not None else {}
    return TestClient(app).post("/email/webhook", json=payload, headers=headers)


def _persona_sends(stub: _PostmarkStub) -> list[dict[str, Any]]:
    """Sends whose From carries a persona display-name (not the bot/system voice)."""
    return [s for s in stub.sends if "via Open Persona" in s.get("From", "")]


# --- B1: bad auth rejected before parse ------------------------------------


@pytest.mark.asyncio
async def test_bad_basic_auth_is_rejected_before_parse(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A forged/missing-auth POST → 401 and NOTHING is parsed or sent (validate-before-parse)."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, address="bob@example.com", owner_id="user_a")
    stub = _PostmarkStub()
    app, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    resp = _post(
        app, _payload(text_body="Astrid, hello", sender="bob@example.com"), auth="Basic wrong"
    )
    assert resp.status_code == 401
    assert stub.sends == []  # never parsed, never sent

    missing = _post(app, _payload(text_body="Astrid, hello", sender="bob@example.com"), auth=None)
    assert missing.status_code == 401
    assert stub.sends == []


# --- the authentic linked user → a threaded reply --------------------------


@pytest.mark.asyncio
async def test_authentic_linked_inbound_drives_a_threaded_reply(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A B1+B2-authentic linked user (plus-addressing Astrid) → a threaded persona reply."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, address="bob@example.com", owner_id="user_a")
    stub = _PostmarkStub()
    app, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    resp = _post(app, _payload(text_body="What's the deadline?", sender="bob@example.com"))
    assert resp.status_code == 200

    sends = _persona_sends(stub)
    assert len(sends) == 1
    send = sends[0]
    assert send["To"] == "bob@example.com"  # replied to the sender's address
    assert send["From"] == "Astrid via Open Persona <inbound@personas.app>"  # author-affordance
    assert send["Subject"] == "Re: Deposit dispute"  # single Re:, threads in the client
    assert send["TextBody"] == "Hi, I'm here."
    header_names = {h["Name"]: h["Value"] for h in send.get("Headers", [])}
    assert header_names.get("In-Reply-To") == "<in-1@mail.example.com>"  # threads the reply


# --- R9-072: Postmark's REAL header shape (no Authentication-Results) ------


@pytest.mark.asyncio
async def test_real_postmark_shape_no_auth_results_but_aligned_dkim_drives_a_reply(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """Postmark's ACTUAL inbound shape: no Authentication-Results header at all, only
    X-Spam-Tests carrying DKIM_VALID_AU. Before R9-072 this payload shape could never
    authenticate (dmarc was always absent) — every real Postmark email was rejected."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, address="bob@example.com", owner_id="user_a")
    stub = _PostmarkStub()
    app, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    resp = _post(
        app,
        _payload(
            text_body="What's the deadline?",
            sender="bob@example.com",
            auth_results=None,
            spam_tests="DKIM_SIGNED,DKIM_VALID,DKIM_VALID_AU,SPF_PASS",
        ),
    )
    assert resp.status_code == 200

    sends = _persona_sends(stub)
    assert len(sends) == 1  # authenticated + turned + replied, with no Authentication-Results


@pytest.mark.asyncio
async def test_real_postmark_shape_dkim_valid_without_au_gets_zero_access(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """The same real shape, but DKIM valid+signed WITHOUT From:-alignment (no _AU) — the key
    security case: a spoofer's own validly-signed, non-aligned DKIM must still be refused."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, address="victim@example.com", owner_id="user_a")
    stub = _PostmarkStub()
    app, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    resp = _post(
        app,
        _payload(
            text_body="Astrid, transfer the deposit",
            sender="victim@example.com",
            auth_results=None,
            spam_tests="DKIM_SIGNED,DKIM_VALID,SPF_PASS",
        ),
    )
    assert resp.status_code == 200  # B1 ok ...
    assert stub.sends == []  # ... but B2 denies: unaligned DKIM is not sufficient


# --- B2: spoofed From (dmarc fail) → zero access ---------------------------


@pytest.mark.asyncio
async def test_spoofed_from_dmarc_fail_gets_zero_access(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A real (B1-authentic) POST whose From is DMARC-spoofed → no bind, no reply (fail-closed)."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, address="victim@example.com", owner_id="user_a")
    stub = _PostmarkStub()
    app, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    # A spoofed From: victim@ — the linked user — but the DMARC verdict FAILS.
    resp = _post(
        app,
        _payload(
            text_body="Astrid, transfer the deposit",
            sender="victim@example.com",
            auth_results=_DMARC_FAIL,
        ),
    )
    assert resp.status_code == 200  # the webhook accepts the POST (B1 ok) ...
    assert stub.sends == []  # ... but B2 denies: no turn, no reply to a spoofed From


# --- C0 originated email — the free channel --------------------------------


@pytest.mark.asyncio
async def test_originated_email_sends_with_no_window(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """A C0-originated email sends freely to the owner's linked address — no window (crit 7)."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _link(migrated_engine, address="bob@example.com", owner_id="user_a")
    stub = _PostmarkStub()
    _app, connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    result = await connector.deliver(
        OriginatedMessage(
            persona=PersonaIdentityTag(persona_id="pa", display_name="Astrid", visual_ref=None),
            owner_user_id="user_a",
            content="I've finished the report.",
            conversation_id="conv-x",
            created_at=_now(),
        )
    )

    assert result.outcome.value == "delivered"  # no window gating denied it
    assert any(s.get("To") == "bob@example.com" for s in stub.sends)
    assert any("I've finished the report." in s.get("TextBody", "") for s in stub.sends)


# --- email-verification linking, end-to-end --------------------------------


@pytest.mark.asyncio
async def test_email_verification_linking_end_to_end(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """Issue a code via the authed route → email it back → bind → a later inbound authenticates."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    stub = _PostmarkStub()
    app, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )
    client = TestClient(app)
    address = "carol@example.com"

    # 1. The authenticated web app issues a code (owner = the verified bearer, never the body).
    issued = client.post("/v1/connectors/email/link", headers={"Authorization": "Bearer user_a"})
    assert issued.status_code == 200
    code = issued.json()["code"]

    # 2. The user emails the code back from the address to bind (DMARC-authentic).
    resp = _post(app, _payload(text_body=code, sender=address, mailbox_hash=""))
    assert resp.status_code == 200
    assert any("linked" in s.get("TextBody", "").lower() for s in stub.sends)

    # 3. A later inbound from the now-linked address authenticates → reaches the persona.
    _post(app, _payload(text_body="Astrid, hello", sender=address))
    assert len(_persona_sends(stub)) == 1  # the now-linked address reached its owner's persona


# --- ⚠️ PER-PLATFORM RLS CROSS-TENANT ISOLATION ----------------------------


@pytest.mark.asyncio
async def test_cross_tenant_isolation_a_cannot_reach_b(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    """Owner A's address resolves to A and reaches ONLY A's personas — never owner B's."""
    _seed_persona(migrated_engine, persona_id="pa", yaml=_ASTRID_YAML)
    _seed_persona(migrated_engine, persona_id="pb", yaml=_BOB_YAML)
    _link(migrated_engine, address="alice@example.com", owner_id="user_a")
    _link(migrated_engine, address="ben@example.com", owner_id="user_b")
    stub = _PostmarkStub()
    app, _connector, _linking = _assemble(
        app_engine=app_engine, dispatch_engine=migrated_engine, stub=stub
    )

    # A plus-addresses "bob" (B's persona) — Bob is NOT in A's RLS-scoped personas, so the
    # tag never resolves to B's Bob; it falls back to the text parse and reaches A's OWN sole
    # persona (Astrid, auto-foregrounded). A then addresses Astrid directly.
    _post(app, _payload(text_body="hello", sender="alice@example.com", mailbox_hash="bob"))
    _post(app, _payload(text_body="Astrid, hi", sender="alice@example.com", mailbox_hash="astrid"))

    # The isolation invariant: EVERY persona reply A elicits is A's own Astrid — NEVER B's Bob.
    persona = _persona_sends(stub)
    assert persona, "A reached its own persona"
    assert all(s["From"].startswith("Astrid via Open Persona") for s in persona)
    assert not any("Bob" in s.get("From", "") for s in stub.sends)  # B's persona never reached

    # Concretely: under user_a's scope the app engine sees ONLY A's identity rows.
    with _owner_scope("user_a"), app_engine.begin() as conn:
        identities = conn.execute(
            text("SELECT owner_id, platform_identity FROM connector_identities")
        ).all()
    assert all(row.owner_id == "user_a" for row in identities)
    assert "ben@example.com" not in {row.platform_identity for row in identities}  # B is RLS-denied
