"""Community personas must not come up voiceless because nobody set a URL (R9-035).

Every voice path in the api (create-time auto-pick, the lazy remap, the boot sweep)
returns early on an empty ``voice_service_url``. Nothing in the community setup ever
set it, while the web client already talked to the local voice service on :8001 for
calls. So the api believed there was no voice service, and every seeded persona got
no voice, on the one edition where the service is guaranteed to be local.
"""

from __future__ import annotations

import pytest
from persona_api.config import APIConfig, Edition


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_VOICE_SERVICE_URL", raising=False)
    monkeypatch.delenv("PERSONA_EDITION", raising=False)


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
    from types import SimpleNamespace

    from persona_api.services.voice_assignment_service import _voice_service_url  # noqa: PLC2701

    assert _voice_service_url(APIConfig(edition=Edition.community)) == "http://localhost:8001"
    # Duck-typed configs (the existing test fixtures) keep working through the fallback.
    assert _voice_service_url(SimpleNamespace(voice_service_url="http://x")) == "http://x"
    assert _voice_service_url(None) == ""
