"""Contract: core's ``store_audit_events`` view never drifts from the api table.

R9-188, the ``calls`` precedent (``test_calls_contract.py``): the API-free voice
runtime selects the Postgres audit backend through
``persona.audit_postgres.PostgresAuditLogger``, which writes via core's OWN
minimal :class:`~sqlalchemy.Table` view because it cannot import the api schema.
This asserts the two Table defs carry the SAME columns, so the voice writer can
never silently diverge from the migrated DDL, and that the api's name for the
logger IS core's class (one implementation, not two). Pure in-memory comparison,
no DB.
"""

from __future__ import annotations

from persona.audit_postgres import PostgresAuditLogger as CoreLogger
from persona.audit_postgres import store_audit_events as core_view
from persona_api.db.audit_loggers import PostgresAuditLogger as ApiLogger
from persona_api.db.models import store_audit_events as api_table


def test_core_store_audit_events_view_matches_api_schema() -> None:
    core_cols = {c.name for c in core_view.c}
    api_cols = {c.name for c in api_table.c}
    assert core_cols == api_cols, (
        f"core store_audit_events view diverged from api schema: "
        f"core-only={core_cols - api_cols}, api-only={api_cols - core_cols}"
    )


def test_api_postgres_audit_logger_is_the_core_implementation() -> None:
    assert ApiLogger is CoreLogger
