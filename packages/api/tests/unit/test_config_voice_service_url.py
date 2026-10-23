"""Community personas must not come up voiceless because nobody set a URL (R9-035).

Every voice path in the api (create-time auto-pick, the lazy remap, the boot sweep)
returns early on an empty ``voice_service_url``. Nothing in the community setup ever
set it, while the web client already talked to the local voice service on :8001 for
calls. So the api believed there was no voice service, and every seeded persona got
no voice, on the one edition where the service is guaranteed to be local.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from fastapi import Request
from persona_api.config import APIConfig, Edition
from persona_api.errors import VoiceServiceUnavailableError

# Imported here, not inside the tests: the tests patch sys.platform, and a module
# first imported under a patched platform can take the wrong branch at import time.
from persona_api.routes.voice import _voice_service_base  # noqa: PLC2701
from persona_api.services.voice_assignment_service import _voice_service_url  # noqa: PLC2701


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_VOICE_SERVICE_URL", raising=False)
    monkeypatch.delenv("PERSONA_EDITION", raising=False)
    # The local default is the Linux and macOS contract; Windows has its own tests below.
    monkeypatch.setattr(sys, "platform", "linux")


def test_community_defaults_to_the_local_voice_service() -> None:
    """THE regression: unset in community must mean the local service, not 'no voice'."""
    cfg = APIConfig(edition=Edition.community)
    assert cfg.effective_voice_service_url() == "http://localhost:8001"


def test_cloud_unset_stays_empty() -> None:
    """Cloud has no implicit voice service; an unset URL still means none is configured."""
    assert APIConfig(edition=Edition.cloud).effective_voice_service_url() == ""


def test_an_explicit_url_always_wins() -> None:
    """The default is a fallback, never an override of the operator's choice."""
    cfg = APIConfig(edition=Edition.community, voice_service_url="https://voice.example.test")
    assert cfg.effective_voice_service_url() == "https://voice.example.test"


def test_the_service_reads_the_url_through_the_effective_accessor() -> None:
    """The service must consult the accessor, not the bare attribute, or the default is dead.

    A helper that only read ``config.voice_service_url`` would pass the config tests
    above while community personas stayed voiceless in production.
    """

    assert _voice_service_url(APIConfig(edition=Edition.community)) == "http://localhost:8001"
    # Duck-typed configs (the existing test fixtures) keep working through the fallback.
    assert _voice_service_url(SimpleNamespace(voice_service_url="http://x")) == "http://x"
    assert _voice_service_url(None) == ""


def test_the_read_aloud_and_dictation_routes_read_the_url_the_same_way() -> None:
    """The sibling of the test above, for the half that was left behind (R9-212).

    ``13e85f5f`` routed the three assignment paths through the accessor and left
    ``routes/voice.py`` reading the bare attribute, so a community install got auto
    picked voices that worked while read aloud and dictation answered 503 before
    touching the database. One accessor, every caller, or the default is dead in the
    places a person actually presses.
    """

    def _request(config: object) -> Request:
        scope = {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(config=config))}
        return Request(scope)  # type: ignore[arg-type]

    assert _voice_service_base(_request(APIConfig(edition=Edition.community))) == (
        "http://localhost:8001"
    )
    # Duck-typed configs (the existing route fixtures) keep working through the fallback.
    assert (
        _voice_service_base(_request(SimpleNamespace(voice_service_url="http://x/"))) == "http://x"
    )


def _voice_request(config: object) -> Request:
    scope = {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(config=config))}
    return Request(scope)


def test_windows_community_has_no_local_voice_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec WIN T5 (D-VOICE-vendor-only): no voice service runs on Windows, so none is assumed."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert APIConfig(edition=Edition.community).effective_voice_service_url() == ""
    cfg = APIConfig(edition=Edition.community, voice_service_url="https://voice.example.test")
    assert cfg.effective_voice_service_url() == "https://voice.example.test"


def test_windows_voice_routes_say_voice_is_not_available_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(VoiceServiceUnavailableError) as caught:
        _voice_service_base(_voice_request(APIConfig(edition=Edition.community)))
    assert caught.value.context["reason"] == "unsupported_platform"
    assert str(caught.value).startswith(
        "Voice is not available on Windows. The voice service runs on Linux and macOS; "
        "to use voice, run it on a Linux or macOS machine and set PERSONA_VOICE_SERVICE_URL."
    )


def test_an_unset_url_off_windows_is_still_not_configured() -> None:

    with pytest.raises(VoiceServiceUnavailableError) as caught:
        _voice_service_base(_voice_request(APIConfig(edition=Edition.cloud)))
    assert caught.value.context["reason"] == "not_configured"
