"""Where a voice call's durable records go, decided once from configuration (R9-188).

Everything the voice runtime writes for the record went to a temp directory. The
agent runner defaulted its ``audit_root`` to
``tempfile.gettempdir()/persona-voice-agent-audit`` and no caller ever passed
anything else, so the four typed stores' writes, the session lifecycle record
(R9-184) and the per-turn VoiceLog rows (R9-185) all landed on the voice machine's
``/tmp`` and were swept by the next deploy or reboot. Nothing failed; the records
simply were not there afterwards.

**The rule.** The voice audit sink follows configuration and is never a temp
directory in a cloud deployment. Two settings decide it, mirroring the api's pair:

* ``PERSONA_VOICE_AUDIT_BACKEND``: ``jsonl`` (default) or ``postgres``.
* ``PERSONA_VOICE_AUDIT_ROOT``: the JSONL directory. Empty means the temp dir.

``postgres`` puts every store-mutation and lifecycle row in the shared
``store_audit_events`` table through :class:`persona.audit_postgres.PostgresAuditLogger`
(the same rows, the same table, the same implementation the api writes with)
using the call's own RLS-scoped engine. It degrades to JSONL when no engine is
available, the same graceful shape ``persona_api.db.audit_factory`` has.

**The VoiceLog stays JSONL** under the resolved root even on the postgres backend:
there is no table for it. ``turn_logs`` is the CHAT turn's tier/model row and
carries none of the per-hop voice anchors, so writing VoiceLog rows there would
mean inventing a schema this finding did not ask for. With the postgres backend a
temp-dir root is therefore a WARNING (the latency rows are still swept) rather
than a failure (the audit record itself is safe in the table).

**Enforced at boot, not documented.** :func:`check_voice_audit_sink` runs from
``persona_voice.http.app.build_app`` when the process will host the agent worker,
so a cloud deployment pointed at a temp dir refuses to start and names both
settings. Community is allowed to use the temp dir, because a local self-host has
no volume to offer and no deploy sweeping it, and says so once.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from persona.audit import JSONLAuditLogger
from persona.errors import PersonaError
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.audit import AuditLogger
    from sqlalchemy import Engine

    from persona_voice.config import VoiceConfig

__all__ = [
    "DEFAULT_TEMP_AUDIT_DIR_NAME",
    "VoiceAuditSink",
    "VoiceAuditSinkMisconfiguredError",
    "build_voice_audit_sink",
    "check_voice_audit_sink",
    "resolve_voice_audit_backend",
    "resolve_voice_audit_root",
]

_LOG = get_logger("voice.audit_sink")

#: The directory under the system temp dir the runner used to hard-code. Kept as
#: the unset default so nothing changes for a local run, and so the boot check has
#: something concrete to reject in cloud.
DEFAULT_TEMP_AUDIT_DIR_NAME = "persona-voice-agent-audit"

#: Roots already warned about, so the community warning is one line per process
#: and not one per call.
_WARNED_ROOTS: set[str] = set()


class VoiceAuditSinkMisconfiguredError(PersonaError):
    """Raised at boot when a cloud voice deployment would audit into a temp dir."""


@dataclass(frozen=True)
class VoiceAuditSink:
    """The one audit destination a call's writers share.

    Attributes:
        audit_logger: The core audit port every consumer writes through: the
            four typed stores, the core-memory and graph stores, and the session
            lifecycle auditor. ONE instance per call by construction: consumers
            take this object, never a root to build their own from.
        root: The resolved JSONL directory. Also where the VoiceLog rows go, on
            either backend.
        backend: The backend actually in force (``jsonl`` or ``postgres``) after
            the no-engine degrade, not merely the one requested.
    """

    audit_logger: AuditLogger
    root: Path
    backend: str

    def voice_log_path(self, persona_id: str) -> Path:
        """The per-persona VoiceLog file for this call (R9-185's sink)."""
        return self.root / f"{persona_id}.voice-turns.jsonl"


def resolve_voice_audit_root(config: VoiceConfig) -> Path:
    """The JSONL directory this configuration asks for.

    An empty ``PERSONA_VOICE_AUDIT_ROOT`` keeps the historical temp-dir location,
    so a local run is unchanged; ``~`` is expanded so an operator can write
    ``~/.persona_audit``.
    """
    raw = config.audit_root.strip()
    if not raw:
        return Path(tempfile.gettempdir()) / DEFAULT_TEMP_AUDIT_DIR_NAME
    return Path(raw).expanduser()


def resolve_voice_audit_backend(config: VoiceConfig, *, engine_available: bool) -> str:
    """``postgres`` only when asked for AND a database handle exists, else ``jsonl``.

    Mirrors ``persona_api.db.audit_factory``: selecting postgres without an engine
    degrades rather than crashing, so a community or DB-less deployment that
    inherits the variable still runs.
    """
    if config.audit_backend.strip().lower() == "postgres" and engine_available:
        return "postgres"
    return "jsonl"


def _is_under_system_tempdir(root: Path) -> bool:
    """Whether ``root`` resolves inside the OS temp directory (the swept place)."""
    temp = Path(tempfile.gettempdir()).resolve()
    try:
        resolved = root.expanduser().resolve()
    except OSError:  # pragma: no cover - resolve() on an unreachable mount
        return False
    return resolved == temp or temp in resolved.parents


def check_voice_audit_sink(config: VoiceConfig, *, engine_available: bool) -> Path:
    """Validate the configured sink and return the resolved root.

    Raises:
        VoiceAuditSinkMisconfiguredError: The deployment is cloud, the backend
            resolves to JSONL, and the root is inside the system temp dir, so the
            records would be written and then swept. The message names both
            settings so the fix needs no source dive.
    """
    root = resolve_voice_audit_root(config)
    backend = resolve_voice_audit_backend(config, engine_available=engine_available)
    if not _is_under_system_tempdir(root):
        return root
    if backend == "jsonl" and config.is_cloud:
        msg = (
            "voice audit would be written to the system temp directory, which this "
            "machine sweeps, so no call would leave a durable record. Set "
            "PERSONA_VOICE_AUDIT_ROOT to a path on a persistent volume, or set "
            "PERSONA_VOICE_AUDIT_BACKEND=postgres to write the shared audit table."
        )
        raise VoiceAuditSinkMisconfiguredError(
            msg,
            context={
                "PERSONA_VOICE_AUDIT_ROOT": str(root),
                "PERSONA_VOICE_AUDIT_BACKEND": config.audit_backend,
                "edition": config.edition,
            },
        )
    if str(root) not in _WARNED_ROOTS:
        _WARNED_ROOTS.add(str(root))
        if backend == "postgres":
            _LOG.warning(
                "voice audit rows go to Postgres, but the per-turn latency log is under "
                "the system temp dir and will be swept: set PERSONA_VOICE_AUDIT_ROOT to "
                "keep it (root={root})",
                root=str(root),
            )
        else:
            _LOG.warning(
                "voice audit is writing to the system temp dir and will be swept: set "
                "PERSONA_VOICE_AUDIT_ROOT to keep it (root={root})",
                root=str(root),
            )
    return root


def build_voice_audit_sink(*, config: VoiceConfig, engine: Engine | None) -> VoiceAuditSink:
    """Compose the call's single audit sink from configuration.

    Called ONCE per call at the agent runner; every consumer takes the returned
    :attr:`VoiceAuditSink.audit_logger`, so one call has one audit destination and
    one instance behind it.

    Args:
        config: The voice service settings (the two audit knobs live here).
        engine: The call's RLS-scoped engine, or ``None`` when there is none.
            The postgres backend degrades to JSONL without it.
    """
    root = check_voice_audit_sink(config, engine_available=engine is not None)
    backend = resolve_voice_audit_backend(config, engine_available=engine is not None)
    if backend == "postgres" and engine is not None:
        from persona.audit_postgres import PostgresAuditLogger

        return VoiceAuditSink(
            audit_logger=PostgresAuditLogger(engine), root=root, backend="postgres"
        )
    return VoiceAuditSink(audit_logger=JSONLAuditLogger(root), root=root, backend="jsonl")
