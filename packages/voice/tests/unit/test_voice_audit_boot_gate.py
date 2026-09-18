"""A cloud voice process that would audit into the temp dir refuses to boot (R9-188).

The rule has to bite where a deploy notices it: at process start, not on the
first call and not only in a document. ``build_app`` is the voice service's boot,
so these drive the real factory rather than the checker underneath it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona_voice.agent.audit_sink import VoiceAuditSinkMisconfiguredError
from persona_voice.config import VoiceConfig
from persona_voice.http.app import build_app

#: Not under the system temp dir, and never written to (see the sink tests).
_PERSISTENT_ROOT = Path("/data/persona-voice-audit")


def test_cloud_agent_worker_refuses_to_boot_into_the_tempdir() -> None:
    config = VoiceConfig(edition="cloud", agent_inprocess=True, audit_root="")

    with pytest.raises(VoiceAuditSinkMisconfiguredError) as excinfo:
        build_app(config)

    assert "PERSONA_VOICE_AUDIT_ROOT" in str(excinfo.value)


def test_cloud_agent_worker_boots_with_a_persistent_root() -> None:
    config = VoiceConfig(edition="cloud", agent_inprocess=True, audit_root=str(_PERSISTENT_ROOT))
    app = build_app(config)
    assert app.state.agent_launcher is not None


def test_community_agent_worker_boots_on_the_tempdir() -> None:
    """A local self-host has no volume to offer; it is warned, not stopped."""
    config = VoiceConfig(edition="community", agent_inprocess=True, audit_root="")
    app = build_app(config)
    assert app.state.agent_launcher is not None


def test_a_token_only_deployment_is_unaffected() -> None:
    """No agent worker here, so this process writes no voice audit at all."""
    config = VoiceConfig(edition="cloud", agent_inprocess=False, audit_root="")
    app = build_app(config)
    assert app.state.agent_launcher is None
