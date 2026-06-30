"""verify_twilio_signature — the mandatory webhook authenticity gate (Spec C4 T3).

Twilio signs each inbound/status POST: the signature base is the full request URL
with the POST params sorted-by-key and concatenated as key+value appended to the
URL; HMAC-SHA1 with the auth token; base64. The check must be constant-time and —
the property that matters most — fail closed: an unset token OR a missing signature
rejects EVERY request rather than falling open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from persona_connectors._twilio.webhook import verify_twilio_signature
from pydantic import SecretStr

_TOKEN = "twilio-auth-token-xyz"  # noqa: S105 — test literal
_URL = "https://example.test/connectors/whatsapp/inbound"
_PARAMS = {"From": "whatsapp:+14155551234", "To": "whatsapp:+14155238886", "Body": "hi"}


def _sign(token: str, url: str, params: dict[str, str]) -> str:
    """Compute the expected X-Twilio-Signature the way Twilio does (the reference impl)."""
    base = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(token.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_valid_signature_is_accepted() -> None:
    """The correctly-computed signature passes (the happy path)."""
    signature = _sign(_TOKEN, _URL, _PARAMS)
    assert verify_twilio_signature(SecretStr(_TOKEN), _URL, _PARAMS, signature) is True


def test_param_order_does_not_matter() -> None:
    """Params are SORTED by key before hashing — caller dict order is irrelevant."""
    signature = _sign(_TOKEN, _URL, _PARAMS)
    reordered = {"Body": "hi", "To": "whatsapp:+14155238886", "From": "whatsapp:+14155551234"}
    assert verify_twilio_signature(SecretStr(_TOKEN), _URL, reordered, signature) is True


def test_tampered_param_is_rejected() -> None:
    """A forged/altered param value no longer matches the signature."""
    signature = _sign(_TOKEN, _URL, _PARAMS)
    tampered = {**_PARAMS, "Body": "transfer all my money"}
    assert verify_twilio_signature(SecretStr(_TOKEN), _URL, tampered, signature) is False


def test_wrong_token_is_rejected() -> None:
    """A signature computed with a different token fails."""
    signature = _sign("a-different-token", _URL, _PARAMS)  # noqa: S106 — test literal
    assert verify_twilio_signature(SecretStr(_TOKEN), _URL, _PARAMS, signature) is False


def test_tampered_url_is_rejected() -> None:
    """The URL is part of the signed base — a spoofed URL fails."""
    signature = _sign(_TOKEN, _URL, _PARAMS)
    other_url = "https://attacker.test/connectors/whatsapp/inbound"
    assert verify_twilio_signature(SecretStr(_TOKEN), other_url, _PARAMS, signature) is False


def test_missing_signature_fails_closed() -> None:
    """An absent X-Twilio-Signature header → reject (never accept unauthenticated)."""
    assert verify_twilio_signature(SecretStr(_TOKEN), _URL, _PARAMS, None) is False


def test_unset_token_fails_closed() -> None:
    """The critical one: no configured token → reject EVERY request (never fall open)."""
    signature = _sign(_TOKEN, _URL, _PARAMS)
    assert verify_twilio_signature(None, _URL, _PARAMS, signature) is False
    assert verify_twilio_signature(None, _URL, _PARAMS, None) is False


def test_uses_constant_time_compare(monkeypatch: object) -> None:
    """The compare goes through hmac.compare_digest, not ``==`` (timing-safe)."""
    import persona_connectors._twilio.webhook as webhook_mod

    calls: list[tuple[str, str]] = []
    real = webhook_mod.hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(webhook_mod.hmac, "compare_digest", spy)  # type: ignore[attr-defined]
    signature = _sign(_TOKEN, _URL, _PARAMS)
    assert verify_twilio_signature(SecretStr(_TOKEN), _URL, _PARAMS, signature) is True
    assert len(calls) == 1
    assert calls[0][0] == signature  # the presented signature is one operand
