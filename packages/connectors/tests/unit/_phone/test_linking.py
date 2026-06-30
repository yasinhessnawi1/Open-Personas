"""The shared phone-number (SMS/WhatsApp) OTP linking carrier (Spec C4 T7, D-C4-5).

The THIRD linking carrier family (after Telegram deep-link and Discord/Slack
OAuth-state): a short textable code, shown in-app, texted back, redeemed inline
over a plain message. Exercised against a FAKE in-memory LinkStore (no DB).

The load-bearing properties verified here:
- the code is **8-char Crockford base32** (~40 bits; no ambiguous I/L/O/U);
- redeem binds the **caller-supplied (webhook-verified) phone number**, NEVER a
  number parsed from the message text;
- single-use + short-TTL (delegated to C1, re-asserted through the carrier);
- a failed redeem is **observable (logged) without leaking the phone number** (PII).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger as _loguru
from persona_connectors._phone.linking import (
    PhoneLinkingService,
    RedeemStatus,
    generate_phone_code,
    normalise_phone_code,
)
from persona_connectors.domain.linking import LinkingService, LinkRecord, LinkToken

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_TTL = timedelta(minutes=10)
_AMBIGUOUS = set("ILOU")


class _FakeLinkStore:
    """Minimal in-memory LinkStore (the persistence port faked for pure-logic tests)."""

    def __init__(self) -> None:
        self.tokens: dict[str, LinkToken] = {}
        self.identities: list[LinkRecord] = []

    def create_token(self, token: LinkToken) -> None:
        self.tokens[token.token_hash] = token

    def get_token_by_hash(self, token_hash: str) -> LinkToken | None:
        return self.tokens.get(token_hash)

    def consume_token(self, token_hash: str, *, now: datetime) -> None:
        tok = self.tokens[token_hash]
        self.tokens[token_hash] = tok.model_copy(update={"status": "consumed", "consumed_at": now})

    def bind_identity(
        self, *, platform: str, platform_identity: str, owner_id: str, now: datetime
    ) -> None:
        self.identities.append(
            LinkRecord(
                platform=platform,
                platform_identity=platform_identity,
                owner_id=owner_id,
                status="active",
                linked_at=now,
            )
        )

    def get_active_identity(self, *, platform: str, platform_identity: str) -> LinkRecord | None:
        for rec in self.identities:
            if (
                rec.platform == platform
                and rec.platform_identity == platform_identity
                and rec.status == "active"
            ):
                return rec
        return None

    def revoke_identity(
        self, *, owner_id: str, platform: str, platform_identity: str, now: datetime
    ) -> None:
        for i, rec in enumerate(self.identities):
            if (
                rec.owner_id == owner_id
                and rec.platform == platform
                and rec.platform_identity == platform_identity
                and rec.status == "active"
            ):
                self.identities[i] = rec.model_copy(update={"status": "revoked", "revoked_at": now})


def _service(platform: str = "whatsapp") -> tuple[PhoneLinkingService, _FakeLinkStore]:
    store = _FakeLinkStore()
    return PhoneLinkingService(linking=LinkingService(store), platform=platform), store


# --- the code format -------------------------------------------------------


def test_generate_phone_code_is_8_char_unambiguous_crockford() -> None:
    for _ in range(200):
        code = generate_phone_code()
        assert len(code) == 8
        assert code == code.upper()
        assert not (set(code) & _AMBIGUOUS)  # never I, L, O, U (transcription-safe)


def test_normalise_folds_case_separators_and_confusions() -> None:
    # lowercase + hyphen/space stripped + O→0, I/L→1 folding
    assert normalise_phone_code(" abcd-2345 ") == "ABCD2345"
    assert normalise_phone_code("oIlO2345") == "01102345"
    # wrong length / non-base32 → not a code
    assert normalise_phone_code("ABC123") is None
    assert normalise_phone_code("hello there friend") is None
    assert normalise_phone_code("") is None


# --- the carrier -----------------------------------------------------------


def test_issue_code_is_textable_and_round_trips_to_a_bind() -> None:
    svc, _ = _service()
    code = svc.issue_code(owner_id="u1", now=_NOW, ttl=_TTL)
    assert normalise_phone_code(code) == code  # what we hand out is itself a valid code
    result = svc.redeem_texted_code(text=code, platform_identity="+15551230000", now=_NOW)
    assert result.status is RedeemStatus.linked
    assert result.owner_id == "u1"


def test_redeem_binds_the_webhook_identity_not_the_text() -> None:
    """The bound identity is the caller-supplied (signature-verified webhook) number,
    never anything in the message body — the C1-D-5 spoofing guard."""
    svc, _ = _service()
    code = svc.issue_code(owner_id="u1", now=_NOW, ttl=_TTL)
    # the text is JUST the code; the identity is supplied separately by the verified webhook
    svc.redeem_texted_code(text=code, platform_identity="+15559999999", now=_NOW)
    assert svc.resolve_owner(platform_identity="+15559999999") == "u1"


def test_non_code_text_is_not_a_link_attempt() -> None:
    svc, _ = _service()
    result = svc.redeem_texted_code(
        text="hello, who is this?", platform_identity="+15551230000", now=_NOW
    )
    assert result.status is RedeemStatus.not_a_link_attempt
    assert result.message is None  # the flow composes the link-instruction itself


def test_unknown_code_fails_closed() -> None:
    svc, _ = _service()
    result = svc.redeem_texted_code(text="ABCD2345", platform_identity="+15551230000", now=_NOW)
    assert result.status is RedeemStatus.failed
    assert result.message  # a friendly retry line


def test_expired_code_is_rejected() -> None:
    svc, _ = _service()
    code = svc.issue_code(owner_id="u1", now=_NOW, ttl=_TTL)
    later = _NOW + timedelta(minutes=11)
    result = svc.redeem_texted_code(text=code, platform_identity="+15551230000", now=later)
    assert result.status is RedeemStatus.failed


def test_code_is_single_use() -> None:
    svc, _ = _service()
    code = svc.issue_code(owner_id="u1", now=_NOW, ttl=_TTL)
    svc.redeem_texted_code(text=code, platform_identity="+15551230000", now=_NOW)
    again = svc.redeem_texted_code(text=code, platform_identity="+15550000001", now=_NOW)
    assert again.status is RedeemStatus.failed


def test_failed_redeem_is_logged_without_leaking_the_number() -> None:
    svc, _ = _service()
    records: list[str] = []
    sink_id = _loguru.add(records.append, level="WARNING")
    try:
        svc.redeem_texted_code(text="ABCD2345", platform_identity="+15551230000", now=_NOW)
    finally:
        _loguru.remove(sink_id)
    blob = "".join(records)
    assert blob  # a warning WAS emitted (abuse is observable)
    assert "+15551230000" not in blob  # but the raw phone number is NEVER logged (PII)
