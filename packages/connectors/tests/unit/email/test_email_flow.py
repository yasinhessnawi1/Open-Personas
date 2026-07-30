"""EmailInboundFlow (Spec C5, Group E) — the B2 gate + redeem + attachment branches, isolated.

The integration test drives the happy paths through the real mounted app; these isolate the
branch edges with lightweight fakes: B2 fail → zero access, a failed OTP redeem, the unlinked
fall-through to the shared flow's link-instruction, and the linked attachment-only ack.
"""
# ruff: noqa: ARG002 — the fakes mirror real protocol signatures; unused params are intentional.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona_connectors._phone.linking import RedeemResult, RedeemStatus
from persona_connectors.domain.normalise import NormalisedInbound
from persona_connectors.email.flow import EmailInboundFlow
from persona_connectors.email.inbound import ParsedEmail
from persona_connectors.errors import IdentityNotLinkedError

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
_PASS = "mx.postmark.com; dkim=pass; spf=pass; dmarc=pass (p=REJECT)"
_FAIL = "mx.postmark.com; dkim=fail; spf=softfail; dmarc=fail"
# Postmark's real signal (no Authentication-Results at all): the aligned DKIM_VALID_AU token.
_SPAM_TESTS_ALIGNED = "DKIM_SIGNED,DKIM_VALID,DKIM_VALID_AU,SPF_PASS"
_SPAM_TESTS_UNALIGNED = "DKIM_SIGNED,DKIM_VALID,SPF_PASS"


class _FakeConnector:
    def __init__(self) -> None:
        self.system: list[dict[str, object]] = []
        self.replies: list[dict[str, object]] = []

    async def send_system_email(self, **kwargs: object) -> None:
        self.system.append(kwargs)

    async def send_reply(self, **kwargs: object) -> None:
        self.replies.append(kwargs)


class _FakeShared:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def handle_text(
        self,
        inbound: NormalisedInbound,
        *,
        transport: object,
        envelope_persona_tag: str | None = None,
    ) -> None:
        self.calls.append((inbound.text, envelope_persona_tag))


class _FakeLinking:
    def __init__(
        self,
        *,
        linked: bool = False,
        status: RedeemStatus = RedeemStatus.not_a_link_attempt,
        message: str | None = None,
    ) -> None:
        self._linked = linked
        self._status = status
        self._message = message

    def resolve_owner(self, *, platform_identity: str) -> str:
        if self._linked:
            return "user_a"
        raise IdentityNotLinkedError("unlinked", context={})

    def redeem_emailed_code(
        self, *, text: str, platform_identity: str, now: datetime
    ) -> RedeemResult:
        owner = "user_a" if self._status is RedeemStatus.linked else None
        return RedeemResult(status=self._status, owner_id=owner, message=self._message)


def _parsed(
    *,
    text: str,
    auth: str | None = _PASS,
    spam_tests: str | None = None,
    tag: str | None = "astrid",
    attachments: bool = False,
) -> ParsedEmail:
    inbound = NormalisedInbound(
        platform="email",
        sender_id="bob@example.com",
        conversation_key="root@x",
        message_id="<in-1@x>",
        text=text,
        received_at=_NOW,
    )
    return ParsedEmail(
        inbound=inbound,
        envelope_persona_tag=tag,
        subject="Re: Hi",
        references="<root@x>",
        authentication_results=auth,
        spam_tests=spam_tests,
        received_spf=None,
        has_attachments=attachments,
    )


def _flow(
    connector: _FakeConnector, shared: _FakeShared, linking: _FakeLinking
) -> EmailInboundFlow:
    return EmailInboundFlow(
        connector=connector,  # type: ignore[arg-type]
        linking=linking,  # type: ignore[arg-type]
        shared=shared,  # type: ignore[arg-type]
        now=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_b2_fail_gives_zero_access() -> None:
    """A DMARC-failing From → no reply, no bind, the shared flow never runs (fail-closed)."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=True)
    await _flow(connector, shared, linking).handle(_parsed(text="Astrid, hi", auth=_FAIL))
    assert connector.system == []
    assert connector.replies == []
    assert shared.calls == []


@pytest.mark.asyncio
async def test_b2_real_postmark_case_no_auth_results_but_aligned_dkim_passes() -> None:
    """R9-072's actual fix: no Authentication-Results header at all (Postmark's real inbound
    shape) but X-Spam-Tests carries DKIM_VALID_AU → authentic, the shared flow runs."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=True)
    parsed = _parsed(text="Astrid, hi", auth=None, spam_tests=_SPAM_TESTS_ALIGNED)
    await _flow(connector, shared, linking).handle(parsed)
    assert shared.calls == [("Astrid, hi", "astrid")]


@pytest.mark.asyncio
async def test_b2_unaligned_dkim_without_au_still_gives_zero_access() -> None:
    """A validly-signed but NOT From:-aligned DKIM (no _AU) → still zero access."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=True)
    parsed = _parsed(text="Astrid, hi", auth=None, spam_tests=_SPAM_TESTS_UNALIGNED)
    await _flow(connector, shared, linking).handle(parsed)
    assert connector.system == []
    assert connector.replies == []
    assert shared.calls == []


@pytest.mark.asyncio
async def test_linked_text_routes_through_shared_with_envelope_tag() -> None:
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=True)
    await _flow(connector, shared, linking).handle(_parsed(text="What's up?", tag="astrid"))
    assert shared.calls == [("What's up?", "astrid")]


@pytest.mark.asyncio
async def test_unlinked_falls_through_to_shared_link_instruction() -> None:
    """An unlinked sender's non-code message falls through to the shared flow (link-instruction)."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=False)
    await _flow(connector, shared, linking).handle(_parsed(text="hello there"))
    assert len(shared.calls) == 1  # reached the shared flow (which sends the link-instruction)


@pytest.mark.asyncio
async def test_failed_otp_redeem_sends_retry_copy() -> None:
    connector, shared, linking = (
        _FakeConnector(),
        _FakeShared(),
        _FakeLinking(status=RedeemStatus.failed, message="that code didn't work"),
    )
    await _flow(connector, shared, linking).handle(_parsed(text="BADCODE12"))
    assert len(connector.system) == 1
    assert "didn't work" in connector.system[0]["text"]
    assert shared.calls == []  # a failed redeem does not proceed to the flow


@pytest.mark.asyncio
async def test_linked_attachment_only_is_acknowledged() -> None:
    """A linked sender's attachment-only email (empty text) → the graceful ack, no turn."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=True)
    await _flow(connector, shared, linking).handle(_parsed(text="   ", attachments=True))
    assert len(connector.system) == 1
    assert "attachments" in connector.system[0]["text"].lower()
    assert shared.calls == []
