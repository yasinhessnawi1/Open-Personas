"""Env-gated audit-backend selection (Spec R5, R5-D-2).

Mirrors ``app.py::_build_rate_limiter``: one factory per audit port that returns
the JSONL implementation (**default** — community / single-node byte-unchanged)
or the multi-worker-safe Postgres implementation, chosen by
``config.audit_backend`` (``PERSONA_API_AUDIT_BACKEND`` ∈ ``{jsonl, postgres}``).

Lives in a shared module — NOT inside ``app.py`` — because BOTH the API process
(``app.py`` lifespan) AND the background worker (``background/worker_root.py``)
must select the SAME backend (R5-D-2): worker-emitted audit has to land in the
same place as API-emitted audit, else scaling the worker re-opens the very
single-writer hole R5 closes.

``postgres`` requires a live ``Engine``; if the backend is ``postgres`` but no
engine is available (e.g. the community / no-DB path) the factory falls back to
JSONL rather than failing — the same graceful-degradation shape as
``_build_rate_limiter``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from persona.audit import JSONLAuditLogger
from persona.tools.audit import JSONLToolAuditLogger

from persona_api.db.audit_loggers import PostgresAuditLogger, PostgresToolAuditLogger

if TYPE_CHECKING:
    from persona.audit import AuditLogger
    from persona.tools.audit import ToolAuditLogger
    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = ["build_audit_logger", "build_tool_audit_logger"]


def build_audit_logger(config: APIConfig, engine: Engine | None) -> AuditLogger:
    """The store-mutation audit logger: Postgres when configured + an engine
    exists, else the JSONL default (byte-unchanged)."""
    if config.audit_backend == "postgres" and engine is not None:
        return PostgresAuditLogger(engine)
    return JSONLAuditLogger(Path(config.audit_root))


def build_tool_audit_logger(config: APIConfig, engine: Engine | None) -> ToolAuditLogger:
    """The tool audit logger: Postgres when configured + an engine exists, else
    the JSONL default (byte-unchanged)."""
    if config.audit_backend == "postgres" and engine is not None:
        return PostgresToolAuditLogger(engine)
    return JSONLToolAuditLogger(Path(config.audit_root))
