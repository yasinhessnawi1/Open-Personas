"""Community managed-Postgres substrate — the invisible product-owned DB (Spec K10, T1).

The owner directive (K10-R-A/B/C): the community edition gets the FULL memory
stack — graph, K7 versioning, K8 episodic engine, consolidation, recall — with
**no SQLite graph port**. Community instead moves onto a **product-managed
Postgres + pgvector** substrate the user never installs, configures, or manages.

This module owns that substrate's lifecycle (T1). Two flavors behind one
:class:`CommunityDbManager`, chosen by the D-K10-1 resolution order
(external ``DATABASE_URL`` → embedded):

- **external** — the user (a self-host power user) set ``DATABASE_URL``; we use it
  as-is and only migrate-on-boot. Zero new process to own.
- **embedded** — the default invisible path: a pip-bundled Postgres 16 + pgvector
  (`pixeltable-pgserver`, D-K10-1) provisioned under ``~/.persona/pg16/``, started
  on boot (reattaching to an already-running postmaster — D-K10-9), and stopped on
  app shutdown via an explicit ``pg_ctl stop -m fast`` (the manager owns the stop
  because ``get_server(cleanup_mode=None)`` does NOT stop it — Phase-2 gotcha #1).

Connection posture (D-K10-2): community connects as the embedded instance's
**superuser**, so the Alembic chain's ``FORCE ROW LEVEL SECURITY`` policies are
inert (RLS is a multi-tenant control; community is single-owner and correctness
comes from the ``WHERE owner_id = …`` predicates the transports already carry).

Datadir contract (D-K10-10): ``~/.persona/pg16/`` — short (keeps the unix-socket
path under the AF_UNIX ``sun_path`` cap) and PG-major version-stamped; a datadir
whose ``PG_VERSION`` disagrees with the bundled major is **refused, not
corrupted** (a future major bump lands under a new ``pgNN`` dir; a dump/restore
upgrade path is a documented follow-up, not built here).

Migrations (D-K10-8): the SAME Alembic chain as cloud runs on **every boot**
(no-op at head), so an app-update migration self-applies with zero user action —
the invisible-DB contract. :func:`run_migrations` sets ``sqlalchemy.url`` on the
Alembic ``Config`` **and** ``DATABASE_URL`` in the environment (Phase-2 gotcha #4:
``alembic/env.py`` lets the env var win, so both must agree).
"""

from __future__ import annotations

import os
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from persona.logging import get_logger
from sqlalchemy import create_engine, text

from persona_api.errors import CommunityDbError


class _EmbeddedServer(Protocol):
    """The slice of ``pixeltable_pgserver.PostgresServer`` this module drives.

    A structural type over the untyped embedded-Postgres package so the manager
    stays typed (mypy-strict clean) without leaking ``Any`` through its surface.
    """

    def get_uri(self, database: str | None = ...) -> str: ...
    def ensure_postgres_running(self) -> None: ...
    def get_pid(self) -> int | None: ...


__all__ = [
    "EMBEDDED_PG_MAJOR",
    "CommunityDbManager",
    "CommunityDbMode",
    "managed_database_name",
    "resolve_managed_external_url",
    "run_migrations",
    "to_sync_psycopg_url",
    "versioned_datadir",
]

_LOG = get_logger("api.db.community_managed")

#: The Postgres major bundled by the pinned ``pixeltable-pgserver`` (0.5.1 → PG 16;
#: verified 16.11 in the Phase-2 spike). The datadir is stamped with this major so
#: a future bump lands under a NEW ``pgNN`` dir rather than reusing an old-major
#: datadir (D-K10-10 refuse-don't-corrupt).
EMBEDDED_PG_MAJOR = 16

#: The application database created inside the managed instance. A constant literal
#: (never user input), so interpolating it into ``CREATE DATABASE`` is injection-safe.
_DB_NAME = "persona"

#: AF_UNIX ``sun_path`` is 104 bytes on macOS (108 on Linux) including the trailing
#: NUL; use the tighter macOS bound. The postmaster's socket file is
#: ``<datadir>/.s.PGSQL.<port>`` — reserve the longest realistic suffix.
_MAX_UNIX_SOCKET_PATH = 103
_SOCKET_SUFFIX = "/.s.PGSQL.65535"


class CommunityDbMode(StrEnum):
    """How the community edition sources its relational + vector store (D-K10-1/-5).

    ``legacy_sqlite`` is today's zero-infra SQLite + Chroma path — **deprecated**
    (D-K10-5), retained one release strictly as the auto-import source + rollback
    target. The managed modes are the K10 deliverable; ``auto`` follows the
    D-K10-1 resolution order (external ``DATABASE_URL`` → embedded). (The
    legacy-detect-and-import step ahead of ``auto`` lands in T6; until then
    ``auto`` selects the managed substrate directly.)
    """

    legacy_sqlite = "legacy-sqlite"
    external = "external"
    embedded = "embedded"
    auto = "auto"


def to_sync_psycopg_url(url: str) -> str:
    """Coerce a Postgres DSN to the sync ``postgresql+psycopg://`` dialect (D-07-1).

    ``pixeltable-pgserver`` hands back a bare ``postgresql://`` URI; the request
    engine + Alembic runner want the sync psycopg3 driver. An ``+asyncpg`` DSN a
    user set is coerced to sync too, mirroring ``db/engine._sync_url``.
    """
    coerced = url.replace("+asyncpg", "+psycopg")
    if coerced.startswith("postgresql://"):
        coerced = coerced.replace("postgresql://", "postgresql+psycopg://", 1)
    elif coerced.startswith("postgres://"):
        coerced = coerced.replace("postgres://", "postgresql+psycopg://", 1)
    return coerced


def managed_database_name() -> str:
    """The application database name created inside the managed instance."""
    return _DB_NAME


def versioned_datadir(base_dir: Path) -> Path:
    """The PG-major version-stamped datadir under ``base_dir`` (D-K10-10).

    ``~/.persona`` → ``~/.persona/pg16``. Keeping the major in the path is what
    makes a future upgrade land in a fresh directory instead of corrupting an
    old-major datadir.
    """
    return base_dir.expanduser() / f"pg{EMBEDDED_PG_MAJOR}"


def resolve_managed_external_url(mode: CommunityDbMode, database_url: str) -> str | None:
    """Resolve the D-K10-1 order → the external DSN, or ``None`` meaning embedded.

    - ``external``: ``database_url`` is required (refuse if unset — the user asked
      for external but gave nothing to connect to).
    - ``embedded``: always embedded (any ``database_url`` is ignored).
    - ``auto``: ``database_url`` when set, else embedded.

    Raises:
        CommunityDbError: ``mode`` is ``external`` with an empty ``database_url``,
            or ``mode`` is ``legacy_sqlite`` (the SQLite path is not managed here).
    """
    if mode is CommunityDbMode.legacy_sqlite:
        msg = "resolve_managed_external_url called for the legacy-sqlite mode"
        raise CommunityDbError(msg, context={"reason": "not_managed", "mode": mode.value})
    if mode is CommunityDbMode.external:
        if not database_url:
            raise CommunityDbError(
                "refusing to start: PERSONA_COMMUNITY_DB_MODE=external but DATABASE_URL is unset. "
                "Set DATABASE_URL to your Postgres DSN, or use the embedded/auto mode for the "
                "bundled zero-infra database.",
                context={"reason": "external_url_unset", "mode": mode.value},
            )
        return to_sync_psycopg_url(database_url)
    if mode is CommunityDbMode.embedded:
        return None
    # auto
    return to_sync_psycopg_url(database_url) if database_url else None


def guard_socket_path_length(datadir: Path) -> None:
    """Refuse a datadir whose unix-socket path would exceed the AF_UNIX cap (D-K10-10).

    The embedded postmaster listens ONLY on a unix socket in ``datadir``; if the
    path is too long the socket silently fails at connect. Refuse loudly instead.
    """
    socket_len = len(str(datadir)) + len(_SOCKET_SUFFIX)
    if socket_len > _MAX_UNIX_SOCKET_PATH:
        raise CommunityDbError(
            "refusing to start: the managed-Postgres datadir path is too long for a unix "
            f"socket ({socket_len} > {_MAX_UNIX_SOCKET_PATH} chars). The embedded database "
            "listens on a socket inside the datadir; choose a shorter "
            "PERSONA_API_COMMUNITY_MANAGED_DB_DIR (default ~/.persona).",
            context={
                "reason": "socket_path_too_long",
                "path": str(datadir),
                "socket_len": str(socket_len),
            },
        )


def guard_datadir_major(datadir: Path, *, expected_major: int) -> None:
    """Refuse-don't-corrupt: an existing datadir must match the bundled major (D-K10-10).

    A provisioned datadir carries a ``PG_VERSION`` file with its major. If the
    bundled binary's major differs, pointing it at this datadir would corrupt it —
    so refuse. A fresh (non-existent) datadir passes (nothing to corrupt).
    """
    version_file = datadir / "PG_VERSION"
    if not version_file.exists():
        return
    found = version_file.read_text(encoding="utf-8").strip()
    if found != str(expected_major):
        raise CommunityDbError(
            "refusing to start: the managed-Postgres datadir was created by Postgres major "
            f"{found}, but this build bundles major {expected_major}. Pointing the new binary at "
            "an old-major datadir would corrupt it. A future release will ship a dump/restore "
            "upgrade path; for now, move the old datadir aside to start fresh.",
            context={
                "reason": "pg_major_mismatch",
                "path": str(datadir),
                "found_major": found,
                "expected_major": str(expected_major),
            },
        )


def _find_alembic_dir() -> Path:
    """Locate the ``packages/api`` dir holding ``alembic.ini`` (walk up from here).

    Community runs from the uv-workspace checkout, so ``packages/api/alembic`` is
    always on disk; walking up from this module is robust to the exact depth.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").exists():
            return parent
    raise CommunityDbError(
        "could not locate alembic.ini to migrate the managed database",
        context={"reason": "alembic_ini_not_found"},
    )


def run_migrations(database_url: str) -> None:
    """Run the SAME Alembic chain as cloud to head, every boot (D-K10-8).

    A no-op when already at head. Sets ``sqlalchemy.url`` on the Alembic ``Config``
    AND ``DATABASE_URL`` in the environment (Phase-2 gotcha #4: ``alembic/env.py``
    lets the env var win over the ini, so both must name the managed DB).
    """
    from alembic import command
    from alembic.config import Config

    api_dir = _find_alembic_dir()
    os.environ["DATABASE_URL"] = database_url  # env.py prefers this over the ini
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


class CommunityDbManager:
    """Owns the community managed-Postgres substrate lifecycle (Spec K10, T1).

    Construct with ``external_url`` set (external mode — the user's DSN, no process
    owned) or ``None`` (embedded mode — a bundled Postgres provisioned under
    ``base_dir``). :meth:`start` provisions/reattaches and returns the sync
    ``postgresql+psycopg://`` DSN the request engine + migrations run on;
    :meth:`stop` stops an embedded postmaster (a no-op in external mode).

    Not thread-safe; the lifespan owns exactly one instance and calls
    ``start()``/``stop()`` once each.
    """

    def __init__(self, *, external_url: str | None, base_dir: Path) -> None:
        self._external_url = external_url
        self._base_dir = base_dir
        self._datadir: Path | None = None
        self._server: _EmbeddedServer | None = None
        self._database_url: str | None = None

    @property
    def is_embedded(self) -> bool:
        """Whether this manager owns an embedded postmaster (vs an external DSN)."""
        return self._external_url is None

    @property
    def database_url(self) -> str:
        """The sync DSN the store engine runs on. Valid only after :meth:`start`."""
        if self._database_url is None:
            msg = "CommunityDbManager.start() has not been called"
            raise CommunityDbError(msg, context={"reason": "not_started"})
        return self._database_url

    def start(self) -> str:
        """Provision/reattach the substrate and return its sync DSN (D-K10-1/-9/-10)."""
        if self._external_url is not None:
            self._database_url = self._external_url
            _LOG.info("community managed DB: using external DATABASE_URL")
            return self._database_url
        return self._start_embedded()

    def _start_embedded(self) -> str:
        datadir = versioned_datadir(self._base_dir)
        guard_socket_path_length(datadir)
        guard_datadir_major(datadir, expected_major=EMBEDDED_PG_MAJOR)
        # ``get_server`` creates the datadir but its PARENT must already exist.
        datadir.parent.mkdir(parents=True, exist_ok=True)
        try:
            import pixeltable_pgserver
        except ImportError as exc:  # pragma: no cover — the dep is a hard requirement
            raise CommunityDbError(
                "the embedded Postgres package (pixeltable-pgserver) is not installed; "
                "reinstall the app dependencies or set DATABASE_URL to use an external Postgres.",
                context={"reason": "embedded_package_missing"},
            ) from exc

        _LOG.info(
            "community managed DB: provisioning/reattaching embedded Postgres at {dir}",
            dir=str(datadir),
        )
        # cleanup_mode=None: WE own the stop (Phase-2 gotcha #1 — cleanup() won't
        # stop the postmaster in this mode). ``get_server`` reattaches to an
        # already-running postmaster for this datadir (D-K10-9 reattach-if-found).
        # ``pixeltable_pgserver`` ships no py.typed and does not re-export via __all__.
        server: _EmbeddedServer = pixeltable_pgserver.get_server(  # type: ignore[attr-defined]
            datadir, cleanup_mode=None
        )
        # ``get_server`` caches the handle per datadir (``PostgresServer._instances``),
        # so after OUR explicit ``pg_ctl stop`` (D-K10-9) a later boot in the SAME
        # process gets a cached handle whose postmaster is down. ``ensure_postgres_running``
        # is the idempotent guarantee it is actually up — a no-op on a live server,
        # a restart on a stopped/crashed one (reattach-if-found made robust).
        server.ensure_postgres_running()
        self._server = server
        self._datadir = datadir
        self._ensure_database(server)
        self._database_url = to_sync_psycopg_url(server.get_uri(database=_DB_NAME))
        return self._database_url

    def _ensure_database(self, server: _EmbeddedServer) -> None:
        """Create the ``persona`` database if absent (idempotent, autocommit)."""
        admin_url = to_sync_psycopg_url(server.get_uri(database="postgres"))
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with admin_engine.connect() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": _DB_NAME},
                ).scalar()
                if not exists:
                    # _DB_NAME is a constant literal — safe to interpolate.
                    conn.execute(text(f'CREATE DATABASE "{_DB_NAME}"'))
                    _LOG.info("community managed DB: created database {name}", name=_DB_NAME)
        finally:
            admin_engine.dispose()

    def stop(self) -> None:
        """Stop an embedded postmaster via ``pg_ctl stop -m fast`` (D-K10-9).

        Best-effort and idempotent — a teardown failure must never crash shutdown.
        A no-op in external mode (we never started that server).
        """
        if self._external_url is not None or self._datadir is None:
            return
        pg_ctl = self._pg_ctl_path()
        if pg_ctl is None:
            _LOG.warning("community managed DB: pg_ctl not found; leaving postmaster running")
            return
        try:
            result = subprocess.run(  # noqa: S603 — fixed bundled binary, no shell
                [str(pg_ctl), "stop", "-D", str(self._datadir), "-m", "fast"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                _LOG.info("community managed DB: embedded Postgres stopped")
            else:
                _LOG.warning(
                    "community managed DB: pg_ctl stop returned {rc}: {err}",
                    rc=result.returncode,
                    err=(result.stderr or "").strip(),
                )
        except Exception as exc:  # noqa: BLE001 — shutdown must never crash
            _LOG.warning("community managed DB: pg_ctl stop failed: {err}", err=str(exc))
        finally:
            self._server = None

    @staticmethod
    def _pg_ctl_path() -> Path | None:
        """Locate the bundled ``pg_ctl`` binary shipped inside pixeltable-pgserver."""
        try:
            import pixeltable_pgserver
        except ImportError:  # pragma: no cover
            return None
        candidate = Path(pixeltable_pgserver.__file__).parent / "pginstall" / "bin" / "pg_ctl"
        return candidate if candidate.exists() else None
