"""``persona audit <path>`` — pretty-printed JSONL audit log viewer."""
# ruff: noqa: B008 — typer.Argument/Option in defaults is the framework idiom

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 — typer needs runtime access
from typing import cast, get_args

import typer

from persona.audit import AuditAction, JSONLAuditLogger, StoreKind
from persona.config import PersonaCoreConfig
from persona.schema.chunks import WriteSource
from persona.schema.persona import Persona

__all__ = ["audit"]

# Derived from the Literal itself (never a hand-copied tuple) so this filter
# can never drift from persona.audit.StoreKind again — R9-031 found exactly
# that drift here: hardcoded to the original four typed stores, silently
# rejecting --store episodic_gist/knowledge_graph/skill/core_memory once each
# was added upstream (the same class of gap the Literal itself had).
_STORE_KINDS: tuple[str, ...] = get_args(StoreKind)

_DURATION_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[smhd])$")
_DURATION_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
}


def audit(
    persona_path: Path = typer.Argument(
        ..., help="Path to the persona YAML whose audit log we should read."
    ),
    since: str | None = typer.Option(
        None, "--since", help='Show events newer than DURATION (e.g. "7d", "2h", "30m").'
    ),
    action: str | None = typer.Option(
        None, "--action", help="Filter to one of: write, delete, remove_documents, rollback."
    ),
    source: str | None = typer.Option(
        None, "--source", help="Filter to one of: system, user, persona_self."
    ),
    store: str | None = typer.Option(
        None, "--store", help=f"Filter to one of: {', '.join(_STORE_KINDS)}."
    ),
) -> None:
    """Print audit events for ``persona_path``, applying optional filters."""
    persona = Persona.from_yaml(persona_path)
    persona_id = persona.persona_id
    if persona_id is None:
        typer.echo("persona has no persona_id; cannot resolve audit log path", err=True)
        raise typer.Exit(code=1)

    config = PersonaCoreConfig()
    audit_root = _resolve_audit_root(config)
    logger = JSONLAuditLogger(audit_root)

    parsed_since = _parse_since(since) if since else None
    parsed_action = AuditAction(action) if action else None
    parsed_source = WriteSource(source) if source else None
    parsed_store: StoreKind | None = None
    if store:
        if store not in _STORE_KINDS:
            raise typer.BadParameter(
                f"invalid --store {store!r}; expected one of {', '.join(_STORE_KINDS)}"
            )
        parsed_store = cast("StoreKind", store)

    events = logger.read(
        persona_id,
        since=parsed_since,
        action=parsed_action,
        source=parsed_source,
        store=parsed_store,
    )

    if not events:
        typer.echo("no audit events match the filter")
        return

    for event in events:
        chunk_summary = f"{len(event.chunk_ids)} chunk(s)" if event.chunk_ids else "-"
        typer.echo(
            f"{event.timestamp.isoformat()} "
            f"{event.action.value:<15} "
            f"{event.store:<12} "
            f"{event.source.value:<13} "
            f"by={event.written_by or '-':<20} "
            f"{chunk_summary} "
            f"reason={event.reason or '-'}",
        )


def _resolve_audit_root(config: PersonaCoreConfig) -> Path:
    if config.audit_path is not None:
        return config.audit_path
    return config.chroma_path / "audit"


def _parse_since(spec: str) -> datetime:
    """Parse "7d" / "2h" / "30m" / "45s" into a UTC datetime in the past."""
    match = _DURATION_RE.match(spec.strip())
    if not match:
        msg = f"invalid --since value {spec!r}; expected e.g. 7d, 2h, 30m, 45s"
        raise typer.BadParameter(msg)
    n = int(match.group("n"))
    unit = match.group("unit")
    delta = timedelta(seconds=n * _DURATION_UNIT_SECONDS[unit])
    return datetime.now(UTC) - delta
