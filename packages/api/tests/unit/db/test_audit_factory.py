"""R5-D-2: the audit-backend factory selects JSONL by default, Postgres by env.

The load-bearing regression for the wiring: with ``PERSONA_API_AUDIT_BACKEND``
unset (its default) the API + worker must keep constructing the JSONL loggers —
community / single-node stays byte-unchanged. Only an explicit ``postgres`` (with
an engine present) swaps in the multi-worker-safe Postgres loggers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.audit import JSONLAuditLogger
from persona.tools.audit import JSONLToolAuditLogger
from persona_api.config import APIConfig
from persona_api.db.audit_factory import build_audit_logger, build_tool_audit_logger
from persona_api.db.audit_loggers import PostgresAuditLogger, PostgresToolAuditLogger

if TYPE_CHECKING:
    import pytest


def test_default_backend_is_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_API_AUDIT_BACKEND", raising=False)
    config = APIConfig()
    assert config.audit_backend == "jsonl"
    # A truthy sentinel stands in for a live Engine — the default path must NOT
    # consult it (it returns JSONL regardless), so any object is safe here.
    engine = object()
    assert isinstance(build_audit_logger(config, engine), JSONLAuditLogger)
    assert isinstance(build_tool_audit_logger(config, engine), JSONLToolAuditLogger)


def test_postgres_backend_selected_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_API_AUDIT_BACKEND", "postgres")
    config = APIConfig()
    assert config.audit_backend == "postgres"
    engine = object()  # PostgresAuditLogger only stores it; never touched here.
    assert isinstance(build_audit_logger(config, engine), PostgresAuditLogger)
    assert isinstance(build_tool_audit_logger(config, engine), PostgresToolAuditLogger)


def test_postgres_requested_but_no_engine_falls_back_to_jsonl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graceful degradation (mirrors _build_rate_limiter): backend=postgres with
    no engine (community / no-DB) falls back to JSONL rather than crashing."""
    monkeypatch.setenv("PERSONA_API_AUDIT_BACKEND", "postgres")
    config = APIConfig()
    assert isinstance(build_audit_logger(config, None), JSONLAuditLogger)
    assert isinstance(build_tool_audit_logger(config, None), JSONLToolAuditLogger)
