"""``python -m persona_api.db.community_repair``: repair memory chains in a managed database.

``persona repair --database-url`` is the operator's command, and a community user is not an
operator: the managed Postgres is invisible (Spec K10), its address is a socket in a folder they
never chose, and on most days it is not running at all, because the app starts it. So when a
migration refuses at boot (D-K10-10) the message names this command instead, which finds the same
database the app would and drives the same repair.

Dry run by default, like ``persona repair``, and the same code decides what is safe. It stops the
database afterwards only if it was the one that started it: stopping a postmaster it found running
would pull the database out from under a running app.
"""

from __future__ import annotations

import argparse
import sys

from persona.audit_postgres import PostgresAuditLogger
from persona.cli.repair_cmd import (
    VERSIONED_KINDS,
    NoEmbedder,
    run_forgotten_sweep,
    run_orphan_sweep,
    run_repair,
)
from persona.stores.core_memory import CORE_MEMORY_KIND
from persona.stores.postgres import PostgresBackend
from sqlalchemy import create_engine

from persona_api.config import APIConfig
from persona_api.db.community_managed import (
    CommunityDbManager,
    CommunityDbMode,
    resolve_managed_external_url,
    versioned_datadir,
)
from persona_api.errors import CommunityDbError

__all__ = ["main", "repair_command"]

_MODULE = "persona_api.db.community_repair"


def repair_command(*flags: str) -> str:
    """The exact line a person runs, from the folder they start Open Persona in."""
    return " ".join(("uv run python -m", _MODULE, *flags))


def _manager(config: APIConfig) -> tuple[CommunityDbManager, bool]:
    """The app's own database, and whether it was already running when we got here."""
    external_url = resolve_managed_external_url(
        CommunityDbMode(config.community_db_mode), config.database_url
    )
    manager = CommunityDbManager(
        external_url=external_url, base_dir=config.community_managed_db_dir
    )
    pid_file = versioned_datadir(config.community_managed_db_dir) / "postmaster.pid"
    return manager, pid_file.exists()


def main(argv: list[str] | None = None) -> int:
    """Check the managed database's memory chains, and repair them with ``--apply``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Repair what can be repaired. Otherwise a dry run."
    )
    parser.add_argument(
        "--resolve-raced",
        action="store_true",
        help="Also settle chains two saves raced, keeping every version.",
    )
    parser.add_argument(
        "--forgotten-evidence",
        action="store_true",
        help="Instead, find memory summaries citing something forgotten. Uses no model.",
    )
    parser.add_argument(
        "--orphaned",
        action="store_true",
        help="Instead, find rows whose persona or conversation has been deleted. Uses no model.",
    )
    args = parser.parse_args(argv)

    config = APIConfig()
    try:
        manager, already_running = _manager(config)
    except CommunityDbError:
        sys.stdout.write(
            "This install has no managed database. If it keeps memory in a local store, use "
            "`persona repair <persona.yaml>`.\n"
        )
        return 2

    url = manager.start()
    engine = create_engine(url)
    try:
        backend = PostgresBackend(engine=engine, embedder=NoEmbedder())
        if args.orphaned:
            return run_orphan_sweep(
                backend.orphan_sweep(), apply=args.apply, written_by="community.repair"
            )
        if args.forgotten_evidence:
            return run_forgotten_sweep(
                backend=backend,
                audit_logger=PostgresAuditLogger(engine),
                persona_ids=backend.persona_ids((CORE_MEMORY_KIND,)),
                apply=args.apply,
                written_by="community.repair",
            )
        return run_repair(
            backend=backend,
            audit_logger=PostgresAuditLogger(engine),
            persona_ids=backend.persona_ids(VERSIONED_KINDS),
            apply=args.apply,
            resolve_raced=args.resolve_raced,
            written_by="community.repair",
        )
    finally:
        engine.dispose()
        if not already_running:
            manager.stop()


if __name__ == "__main__":  # pragma: no cover - manual CLI entry
    raise SystemExit(main())
