"""The voice audit sink follows configuration and is never a swept temp dir (R9-188).

Before this, ``build_agent_session`` hard-coded ``tempfile.gettempdir()`` and no
caller could say otherwise, so every typed-store write, the session lifecycle
record and the per-turn VoiceLog rows were written on the voice machine and then
swept. These drive the REAL composition functions the runner calls, not a copy of
their logic: :func:`build_voice_audit_sink` is the one the runner calls at the
line that used to build the temp path, and :func:`check_voice_audit_sink` is the
one ``build_app`` calls at boot.
"""

from __future__ import annotations

import ast
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona.audit import JSONLAuditLogger
from persona.audit_postgres import PostgresAuditLogger
from persona_voice.agent import runner as runner_module
from persona_voice.agent.audit_sink import (
    VoiceAuditSinkMisconfiguredError,
    build_voice_audit_sink,
    check_voice_audit_sink,
    resolve_voice_audit_root,
)
from persona_voice.config import VoiceConfig

if TYPE_CHECKING:
    from sqlalchemy import Engine


#: A root that is NOT under the system temp dir. It is never written to here (the
#: JSONL logger only creates its directory on the first emit), and pytest's own
#: ``tmp_path`` cannot stand in for it: on macOS that lives under the temp dir,
#: which is exactly what the rule rejects.
_PERSISTENT_ROOT = Path("/data/persona-voice-audit")


class _FakeEngine:
    """Stands in for the session RLS engine; the logger only stores it."""


def _engine() -> Engine:
    from typing import cast

    return cast("Engine", _FakeEngine())


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """WARNING+ lines (persona.logging wraps loguru, so this sees our warnings)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _reset_warned_roots() -> Iterator[None]:
    """The warn-once set is process state; keep tests independent of each other."""
    from persona_voice.agent import audit_sink

    audit_sink._WARNED_ROOTS.clear()  # noqa: SLF001 - resetting module state under test
    yield
    audit_sink._WARNED_ROOTS.clear()  # noqa: SLF001


# ---------------------------------------------------------------- backend choice


def test_postgres_backend_composes_the_postgres_logger_over_the_engine() -> None:
    config = VoiceConfig(edition="cloud", audit_backend="postgres", audit_root="")
    sink = build_voice_audit_sink(config=config, engine=_engine())

    assert isinstance(sink.audit_logger, PostgresAuditLogger)
    assert sink.backend == "postgres"


def test_one_logger_instance_is_shared_by_every_consumer() -> None:
    """The four typed stores, the core-memory read and the lifecycle auditor must
    all write through the SAME instance: one call, one audit destination."""
    from persona_voice.session.lifecycle_audit import SessionLifecycleAuditor

    config = VoiceConfig(edition="cloud", audit_backend="postgres")
    sink = build_voice_audit_sink(config=config, engine=_engine())

    stores = runner_module._build_stores(  # noqa: SLF001 - the production builder
        _engine(),
        embedder=None,  # type: ignore[arg-type] - PostgresBackend only stores it
        audit_logger=sink.audit_logger,
    )
    auditor = SessionLifecycleAuditor(audit_logger=sink.audit_logger)

    for store in stores.values():
        assert store._audit is sink.audit_logger  # noqa: SLF001 - white-box sharing check
    assert auditor._audit is sink.audit_logger  # noqa: SLF001


def test_jsonl_backend_with_an_explicit_root_writes_there() -> None:
    root = _PERSISTENT_ROOT
    config = VoiceConfig(edition="cloud", audit_backend="jsonl", audit_root=str(root))

    sink = build_voice_audit_sink(config=config, engine=_engine())

    assert isinstance(sink.audit_logger, JSONLAuditLogger)
    assert sink.backend == "jsonl"
    assert sink.root == root
    # The VoiceLog rows land under the same root (there is no table for them).
    assert sink.voice_log_path("ada") == root / "ada.voice-turns.jsonl"


def test_postgres_requested_without_an_engine_degrades_to_jsonl() -> None:
    config = VoiceConfig(
        edition="cloud", audit_backend="postgres", audit_root=str(_PERSISTENT_ROOT)
    )

    sink = build_voice_audit_sink(config=config, engine=None)

    assert isinstance(sink.audit_logger, JSONLAuditLogger)
    assert sink.backend == "jsonl"


# ------------------------------------------------------------------ the tempdir rule


def test_unset_root_still_resolves_to_the_historical_temp_location() -> None:
    config = VoiceConfig(edition="community", audit_root="")
    assert resolve_voice_audit_root(config) == (
        Path(tempfile.gettempdir()) / "persona-voice-agent-audit"
    )


def test_cloud_with_a_tempdir_root_fails_fast_and_names_both_settings() -> None:
    config = VoiceConfig(edition="cloud", audit_backend="jsonl", audit_root="")

    with pytest.raises(VoiceAuditSinkMisconfiguredError) as excinfo:
        check_voice_audit_sink(config, engine_available=True)

    message = str(excinfo.value)
    assert "PERSONA_VOICE_AUDIT_ROOT" in message
    assert "PERSONA_VOICE_AUDIT_BACKEND" in message


def test_cloud_tempdir_failure_also_stops_the_session_composition() -> None:
    config = VoiceConfig(edition="cloud", audit_backend="jsonl", audit_root="")
    with pytest.raises(VoiceAuditSinkMisconfiguredError):
        build_voice_audit_sink(config=config, engine=_engine())


def test_cloud_with_a_persistent_root_is_fine() -> None:
    config = VoiceConfig(edition="cloud", audit_root=str(_PERSISTENT_ROOT))
    assert check_voice_audit_sink(config, engine_available=True) == _PERSISTENT_ROOT


def test_cloud_with_an_explicit_root_inside_the_tempdir_still_fails(tmp_path: Path) -> None:
    """Explicit is not the same as persistent: pytest's tmp_path lives under the
    system temp dir on this platform, and a deployment pointed there loses the
    record just as surely as the default did."""
    config = VoiceConfig(edition="cloud", audit_root=str(tmp_path / "audit"))
    with pytest.raises(VoiceAuditSinkMisconfiguredError):
        check_voice_audit_sink(config, engine_available=True)


def test_community_with_a_tempdir_root_warns_once_and_proceeds(
    loguru_capture: list[str],
) -> None:
    config = VoiceConfig(edition="community", audit_backend="jsonl", audit_root="")

    first = check_voice_audit_sink(config, engine_available=False)
    second = check_voice_audit_sink(config, engine_available=False)

    assert first == second == resolve_voice_audit_root(config)
    swept = [line for line in loguru_capture if "swept" in line]
    assert len(swept) == 1, loguru_capture


def test_cloud_postgres_with_a_tempdir_root_warns_about_the_latency_log(
    loguru_capture: list[str],
) -> None:
    """The audit rows are safe in the table, but the VoiceLog still lands in /tmp,
    so the deployment is told rather than stopped."""
    config = VoiceConfig(edition="cloud", audit_backend="postgres", audit_root="")

    check_voice_audit_sink(config, engine_available=True)

    assert any("latency" in line for line in loguru_capture), loguru_capture


# ------------------------------------------------------- the runner builds exactly one


def test_the_runner_constructs_no_audit_logger_of_its_own() -> None:
    """Guard for the shape this finding is made of: a consumer that quietly builds
    its own logger writes somewhere configuration never chose. The runner must
    call the sink builder once and pass that object around."""
    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
    built: list[str] = []
    sink_builds = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id in {"JSONLAuditLogger", "PostgresAuditLogger"}:
            built.append(node.func.id)
        if node.func.id == "build_voice_audit_sink":
            sink_builds += 1

    assert built == [], f"the runner builds its own audit logger: {built}"
    assert sink_builds == 1, f"expected one composition of the audit sink, got {sink_builds}"
