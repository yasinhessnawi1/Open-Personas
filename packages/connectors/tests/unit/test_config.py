"""ConnectorConfig — env-driven settings for the persona-connectors service (C1 T1).

Twelve-factor discipline (the Spec 08 APIConfig / V1 VoiceConfig precedent): every
knob lands via environment variables, prefixed ``PERSONA_CONNECTORS_``; the
edition reads the shared, prefix-less ``PERSONA_EDITION`` (Spec 33). The idle
timeout default is C1-D-3 (30 minutes).
"""

from __future__ import annotations

import pytest
from persona_connectors.config import ConnectorConfig


def test_idle_timeout_default_is_30_minutes() -> None:
    """C1-D-3: the idle-timeout default is 30 minutes (low-stakes, tunable)."""
    config = ConnectorConfig()
    assert config.idle_timeout_minutes == 30


def test_idle_timeout_is_env_tunable() -> None:
    """C1-D-3: tunable via PERSONA_CONNECTORS_IDLE_TIMEOUT_MINUTES."""
    config = ConnectorConfig(idle_timeout_minutes=90)
    assert config.idle_timeout_minutes == 90


def test_idle_timeout_must_be_positive() -> None:
    """A non-positive idle timeout is a misconfiguration — fail fast at the boundary."""
    with pytest.raises(ValueError):  # noqa: PT011 — pydantic raises ValidationError (a ValueError)
        ConnectorConfig(idle_timeout_minutes=0)


def test_edition_defaults_to_community(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-infra default (Spec 33): community, single local owner, no auth."""
    # The edition reads the prefix-less, shared PERSONA_EDITION. The api suite's
    # session-scoped fixture exports PERSONA_EDITION=cloud and that process-global
    # var persists into this cross-package full-suite run — so a *default* test
    # must clear it to actually exercise the default (not the ambient env).
    monkeypatch.delenv("PERSONA_EDITION", raising=False)
    config = ConnectorConfig()
    assert config.edition == "community"
    assert config.is_cloud is False


def test_edition_reads_shared_persona_edition_alias() -> None:
    """The edition reads the prefix-less PERSONA_EDITION var (shared with api/web/voice)."""
    config = ConnectorConfig(edition="cloud")
    assert config.edition == "cloud"
    assert config.is_cloud is True


def test_database_url_defaults_empty() -> None:
    """No DB configured by default — the composition root tolerates absence (T1)."""
    config = ConnectorConfig()
    assert config.database_url == ""


# --- Telegram adapter (Spec C2 T1) ---


def test_telegram_credentials_default_to_none() -> None:
    """No Telegram configured by default — token/secret absent until set (D-C2-X-credential)."""
    config = ConnectorConfig()
    assert config.telegram_bot_token is None
    assert config.telegram_webhook_secret is None


def test_telegram_bot_token_is_a_secret() -> None:
    """The bot token is a SecretStr — never rendered in repr/str/logs (D-C2-X-credential)."""
    config = ConnectorConfig(telegram_bot_token="123456:SECRET-BOT-TOKEN")  # noqa: S106 — test literal
    assert config.telegram_bot_token is not None
    # SecretStr masks the value everywhere except an explicit get_secret_value().
    assert "SECRET-BOT-TOKEN" not in repr(config.telegram_bot_token)
    assert "SECRET-BOT-TOKEN" not in str(config)
    assert config.telegram_bot_token.get_secret_value() == "123456:SECRET-BOT-TOKEN"


def test_telegram_webhook_secret_is_a_secret() -> None:
    """The webhook secret is a SecretStr too (validate-before-parse uses it — D-C2-2)."""
    config = ConnectorConfig(telegram_webhook_secret="hook-secret-abc")  # noqa: S106 — test literal
    assert config.telegram_webhook_secret is not None
    assert "hook-secret-abc" not in str(config)
    assert config.telegram_webhook_secret.get_secret_value() == "hook-secret-abc"


def test_telegram_transport_defaults_to_longpoll() -> None:
    """D-C2-1: long-poll is the zero-infra dev default (no public endpoint needed)."""
    config = ConnectorConfig()
    assert config.telegram_transport == "longpoll"


def test_telegram_transport_accepts_webhook() -> None:
    """D-C2-1: webhook is the prod transport."""
    config = ConnectorConfig(telegram_transport="webhook")
    assert config.telegram_transport == "webhook"


def test_telegram_transport_rejects_unknown_mode() -> None:
    """An unknown transport is a misconfiguration — fail fast at the boundary."""
    with pytest.raises(ValueError):  # noqa: PT011 — pydantic raises ValidationError (a ValueError)
        ConnectorConfig(telegram_transport="carrier-pigeon")


def test_telegram_api_base_url_default() -> None:
    """The Bot API base defaults to the public host (overridable for tests/local server)."""
    config = ConnectorConfig()
    assert config.telegram_api_base_url == "https://api.telegram.org"


# --- Twilio WhatsApp + SMS adapters (Spec C4 T1, D-C4-1) ---


def test_twilio_credentials_default_to_none_or_empty() -> None:
    """No Twilio configured by default — auth token absent, addresses empty (D-C4-1)."""
    config = ConnectorConfig()
    assert config.twilio_account_sid == ""
    assert config.twilio_auth_token is None
    assert config.twilio_webhook_auth_token is None
    assert config.twilio_whatsapp_from == ""
    assert config.twilio_sms_from == ""


def test_twilio_account_sid_is_not_a_secret() -> None:
    """The Account SID is a public identifier (not a credential) — a plain str."""
    config = ConnectorConfig(twilio_account_sid="AC0123456789abcdef")
    assert config.twilio_account_sid == "AC0123456789abcdef"


def test_twilio_auth_token_is_a_secret() -> None:
    """The auth token is a SecretStr — never rendered in repr/str/logs (D-C4-1 credential)."""
    config = ConnectorConfig(twilio_auth_token="SUPER-SECRET-AUTH-TOKEN")  # noqa: S106 — test literal
    assert config.twilio_auth_token is not None
    assert "SUPER-SECRET-AUTH-TOKEN" not in repr(config.twilio_auth_token)
    assert "SUPER-SECRET-AUTH-TOKEN" not in str(config)
    assert config.twilio_auth_token.get_secret_value() == "SUPER-SECRET-AUTH-TOKEN"


def test_twilio_webhook_auth_token_is_a_secret() -> None:
    """The webhook signature-validation token is a SecretStr too (T3 fail-closed)."""
    config = ConnectorConfig(twilio_webhook_auth_token="HOOK-VALIDATE-TOKEN")  # noqa: S106 — test literal
    assert config.twilio_webhook_auth_token is not None
    assert "HOOK-VALIDATE-TOKEN" not in str(config)
    assert config.twilio_webhook_auth_token.get_secret_value() == "HOOK-VALIDATE-TOKEN"


def test_twilio_addresses_load_from_kwargs() -> None:
    """The WhatsApp + SMS from-addresses load (whatsapp:+E164 vs bare E.164)."""
    config = ConnectorConfig(
        twilio_whatsapp_from="whatsapp:+14155238886",
        twilio_sms_from="+14155238886",
    )
    assert config.twilio_whatsapp_from == "whatsapp:+14155238886"
    assert config.twilio_sms_from == "+14155238886"


def test_whatsapp_reengagement_template_sid_defaults_empty() -> None:
    """The 24h-window re-engagement template SID is empty until configured (T9/T13)."""
    config = ConnectorConfig()
    assert config.whatsapp_reengagement_template_sid == ""


def test_twilio_api_base_url_default() -> None:
    """The Twilio API base defaults to the public host (overridable for tests)."""
    config = ConnectorConfig()
    assert config.twilio_api_base_url == "https://api.twilio.com"


def test_phone_link_token_ttl_default_is_10_minutes() -> None:
    """The phone-link token TTL defaults to 10 minutes (short-lived, C1-D-5)."""
    config = ConnectorConfig()
    assert config.phone_link_token_ttl_minutes == 10


def test_phone_link_token_ttl_must_be_positive() -> None:
    """A non-positive link-token TTL is a misconfiguration — fail fast at the boundary."""
    with pytest.raises(ValueError):  # noqa: PT011 — pydantic raises ValidationError (a ValueError)
        ConnectorConfig(phone_link_token_ttl_minutes=0)


def test_sms_max_segments_default_is_3() -> None:
    """SMS multi-segment budget defaults to 3 (the splitter's concatenation cap, T12)."""
    config = ConnectorConfig()
    assert config.sms_max_segments == 3


def test_sms_max_segments_must_be_positive() -> None:
    """A non-positive segment budget is a misconfiguration — fail fast at the boundary."""
    with pytest.raises(ValueError):  # noqa: PT011 — pydantic raises ValidationError (a ValueError)
        ConnectorConfig(sms_max_segments=0)
