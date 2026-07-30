"""EmailInboundFlow (Spec C5, Group E) — the B2 gate + redeem + attachment branches, isolated.

The integration test drives the happy paths through the real mounted app; these isolate the
branch edges with lightweight fakes: B2 fail → zero access, a failed OTP redeem, the unlinked
fall-through to the shared flow's link-instruction, and the linked attachment-only ack.
"""
# ruff: noqa: ARG002 — the fakes mirror real protocol signatures; unused params are intentional.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from loguru import logger as _loguru
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


# --- R9-077: every silent early return must NAME itself in the log ---


def _capture(level: str = "INFO") -> tuple[list[str], int]:
    records: list[str] = []
    return records, _loguru.add(records.append, level=level)


@pytest.mark.asyncio
async def test_otp_carrier_branch_is_logged_without_the_address_or_the_code() -> None:
    """The leading suspect for the production drop: an UNLINKED sender whose code-shaped
    message is answered by the carrier and returns — previously with no log at all."""
    connector, shared, linking = (
        _FakeConnector(),
        _FakeShared(),
        _FakeLinking(status=RedeemStatus.failed, message="that code didn't work"),
    )
    records, sink_id = _capture()
    try:
        await _flow(connector, shared, linking).handle(_parsed(text="BADCODE12"))
    finally:
        _loguru.remove(sink_id)

    blob = "".join(records)
    assert "OTP carrier" in blob  # the branch names itself
    assert "redeem=failed" in blob  # …and which outcome it took
    assert "bob@example.com" not in blob  # the address is fingerprinted, never logged
    assert "BADCODE12" not in blob  # and the code never appears


@pytest.mark.asyncio
async def test_linked_empty_body_branch_is_logged() -> None:
    """A LINKED sender whose body strips to nothing: a real drop, now visible."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=True)
    records, sink_id = _capture("WARNING")
    try:
        await _flow(connector, shared, linking).handle(_parsed(text="   ", attachments=False))
    finally:
        _loguru.remove(sink_id)

    blob = "".join(records)
    assert "no usable text" in blob
    assert "attachments=False" in blob
    assert "bob@example.com" not in blob


@pytest.mark.asyncio
async def test_the_accepted_path_is_logged_too() -> None:
    """The positive evidence the operator pass needs: this message DID reach the flow."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=True)
    records, sink_id = _capture()
    try:
        await _flow(connector, shared, linking).handle(_parsed(text="Astrid, hi"))
    finally:
        _loguru.remove(sink_id)

    blob = "".join(records)
    assert "accepted" in blob
    assert "persona_tag=astrid" in blob
    assert "Astrid, hi" not in blob  # never the message content


@pytest.mark.asyncio
async def test_unlinked_non_code_fall_through_is_logged() -> None:
    """The third unlinked shape (not a code) reaches the shared flow — say so."""
    connector, shared, linking = _FakeConnector(), _FakeShared(), _FakeLinking(linked=False)
    records, sink_id = _capture()
    try:
        await _flow(connector, shared, linking).handle(_parsed(text="hello there"))
    finally:
        _loguru.remove(sink_id)

    blob = "".join(records)
    assert "UNLINKED" in blob
    assert "redeem=not_a_link_attempt" in blob
