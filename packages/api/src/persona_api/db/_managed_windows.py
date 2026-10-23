"""The embedded Postgres on Windows: socket only, a password, owner-only files (Spec WIN, T2).

On Linux and macOS the bundled Postgres listens only on a unix socket inside its 0700
datadir. On Windows the bundled launcher (pixeltable-pgserver) would start it on a
loopback TCP port with ``trust`` authentication for every user: any local account or
process could connect as the database superuser. This module owns the Windows
lifecycle instead, so the same guarantee holds there:

- **Socket only.** The server is started with ``-h "" -k <datadir>``: no TCP listener
  at all, only a unix socket inside the datadir (Windows 10 1803 and later).
- **Owner-only files.** The base folder and the datadir are created with a protected
  DACL for this user and SYSTEM applied atomically, inherited by everything inside,
  the socket file included; Windows checks that DACL when a client connects to the
  socket. Postgres's own data-folder ownership check is compiled out on Windows, so an
  existing folder or password file is refused unless this account (or SYSTEM or the
  Administrators group) owns it and no one else was granted access
  (:mod:`persona_api.db._managed_files`, :mod:`persona_api.db._datadir_guard`).
- **No stale server trusted.** A ``postmaster.pid`` left by a crash counts only if its
  PID is still the bundled ``postgres.exe``.
- **A password as well.** ``pg_hba.conf`` has a single ``local ... scram-sha-256`` line
  (no ``host`` line at all). The generated password lives in a libpq password file
  beside the datadir, created owner-only; the connection string names that file
  (``passfile=``) rather than carrying the password, so the password never reaches
  ``DATABASE_URL``, the environment of a child process, or a log line.
- **Existing installs are upgraded.** A datadir created with ``trust`` (by the bundled
  launcher or an older build) is converted on the next start: a running TCP postmaster
  is stopped, the server restarts socket-only, the password is set, and ``pg_hba.conf``
  is rewritten and reloaded.

The Linux and macOS path does not use this module and is unchanged. The module
imports on every platform so its pure parts are tested everywhere; only
:func:`start_socket_only` is Windows-only.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from persona.tools._sandbox import delete_regular_file_nofollow
from sqlalchemy.engine import URL

from persona_api.db._managed_files import (
    create_private_file,
    prepared_folders,
    read_private_file,
    remove_own_file,
)
from persona_api.errors import CommunityDbError

__all__ = [
    "HBA_CONTENT",
    "SOCKET_PORT",
    "SocketTarget",
    "password_file_for",
    "pgpass_line",
    "start_socket_only",
]

#: The socket's port number. It only names the socket file (``.s.PGSQL.5432``); no
#: TCP port is ever opened.
SOCKET_PORT = 5432
_SUPERUSER = "postgres"
_PGPASS_PREFIX = f"*:*:*:{_SUPERUSER}:"
_PASSWORD_BYTES = 32
_TOOL_TIMEOUT_S = 60
_CONNECT_TIMEOUT_S = 10

#: The whole of pg_hba.conf: this user's socket, with a password. No host line.
HBA_CONTENT = (
    "# Written by Persona (Spec WIN). The bundled database accepts connections only on\n"
    "# its unix socket, whose folder only this Windows account can open, and only with\n"
    "# the password kept beside the data folder. There is no host line: no TCP at all.\n"
    "local   all   all   scram-sha-256\n"
)


@dataclass(frozen=True)
class SocketTarget:
    """How to reach the socket-only server. ``password`` is kept out of ``repr``."""

    socket_dir: Path
    port: int
    password_file: Path
    password: str = field(repr=False)

    def sqlalchemy_url(self, database: str) -> str:
        """A sync ``postgresql+psycopg`` DSN for ``database`` over the unix socket.

        The DSN names the password file, never the password itself.
        """
        url = URL.create(
            "postgresql+psycopg",
            username=_SUPERUSER,
            database=database,
            query={
                "host": str(self.socket_dir),
                "port": str(self.port),
                "passfile": str(self.password_file),
            },
        )
        return url.render_as_string(hide_password=False)


def password_file_for(datadir: Path) -> Path:
    """The libpq password file beside the datadir (``~/.persona/pg16.pgpass``)."""
    return datadir.with_name(f"{datadir.name}.pgpass")


def pgpass_line(password: str) -> str:
    """The one line of the password file: any socket, any database, the superuser.

    ``secrets.token_urlsafe`` passwords are ``[A-Za-z0-9_-]``, so nothing needs the
    format's backslash escaping.
    """
    return f"{_PGPASS_PREFIX}{password}\n"


def start_socket_only(datadir: Path, bin_dir: Path) -> SocketTarget:
    """Provision or upgrade the datadir, start it socket-only, and return how to reach it.

    Raises:
        CommunityDbError: the server could not be initialised or started, did not come
            up socket-only, or an existing datadir already requires a password that is
            no longer on disk.
    """
    password_file = password_file_for(datadir)
    # The base folder and the data folder are created owner-only or checked, and the
    # data folder limited to this account, before anything is read from or written to
    # them (Postgres's own checkDataDir is compiled out on Windows). The folders stay
    # held, so nothing above them can be renamed aside, until the postmaster is up.
    with prepared_folders(datadir):
        fresh = not (datadir / "PG_VERSION").exists()
        password = _ensure_password(password_file, datadir, fresh=fresh)
        if fresh:
            _initdb(bin_dir, datadir, password)
        listen = _postmaster_listen_address(datadir, bin_dir)
        if listen:
            # A postmaster started by the bundled launcher listens on TCP: stop it first.
            _run(bin_dir / "pg_ctl.exe", "stop", "-D", str(datadir), "-m", "fast")
            listen = None
        if listen is None:
            _discard_stale_pid_file(datadir, bin_dir)
            _start(bin_dir, datadir)
        if _postmaster_listen_address(datadir, bin_dir) != "":
            raise CommunityDbError(
                "refusing to continue: the bundled database did not start socket-only",
                context={"reason": "not_socket_only", "path": str(datadir)},
            )
    target = SocketTarget(
        socket_dir=datadir, port=SOCKET_PORT, password_file=password_file, password=password
    )
    _enforce_password_auth(datadir, target)
    return target


def _stored_password(password_file: Path) -> str:
    """The password in the libpq password file, or "" when there is no such file.

    Raises:
        CommunityDbError: the file is not ours alone, or holds no superuser line.
    """
    raw = read_private_file(password_file)
    if raw is None:
        return ""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith(_PGPASS_PREFIX) and line[len(_PGPASS_PREFIX) :].strip():
            return line[len(_PGPASS_PREFIX) :].strip()
    raise CommunityDbError(
        f"The password file {password_file} has no password line for the bundled "
        "database. Restore it from a backup, or move it and the data folder aside to "
        "start with a fresh database.",
        context={"reason": "password_file_unreadable", "path": str(password_file)},
    )


def _ensure_password(password_file: Path, datadir: Path, *, fresh: bool) -> str:
    """The stored password, or a new one written owner-only beside the datadir.

    Raises:
        CommunityDbError: the datadir already requires a password and the file is gone.
    """
    stored = _stored_password(password_file)
    if stored:
        return stored
    if not fresh and _hba_requires_password(datadir):
        raise CommunityDbError(
            "refusing to start: the bundled database requires a password, but its password "
            f"file ({password_file.name}) is missing. Restore that file, or move the data "
            "folder aside to start with a fresh database.",
            context={"reason": "password_file_missing", "path": str(password_file)},
        )
    password = secrets.token_urlsafe(_PASSWORD_BYTES)
    create_private_file(password_file, pgpass_line(password).encode("utf-8"))
    return password


def _hba_requires_password(datadir: Path) -> bool:
    try:
        return (datadir / "pg_hba.conf").read_text(encoding="utf-8") == HBA_CONTENT
    except OSError:
        return False


def _initdb(bin_dir: Path, datadir: Path, password: str) -> None:
    """Create the datadir with scram authentication and the superuser's password.

    ``initdb --pwfile`` reads the password from a file, so it is staged owner-only
    beside the datadir for the one call and removed after it.
    """
    staged = datadir.with_name(f"{datadir.name}.initpw")
    remove_own_file(staged)  # a leftover of ours from a crash; anyone else's is refused
    create_private_file(staged, f"{password}\n".encode())
    try:
        _run(
            bin_dir / "initdb.exe",
            "-D",
            str(datadir),
            "-U",
            _SUPERUSER,
            "--encoding=utf8",
            "--auth-local=scram-sha-256",
            "--auth-host=scram-sha-256",
            f"--pwfile={staged}",
        )
    finally:
        remove_own_file(staged)
    (datadir / "pg_hba.conf").write_text(HBA_CONTENT, encoding="utf-8")


def _start(bin_dir: Path, datadir: Path) -> None:
    options = f'-h "" -k "{datadir}" -p {SOCKET_PORT}'
    _run(
        bin_dir / "pg_ctl.exe",
        "-w",
        "-D",
        str(datadir),
        "-l",
        str(datadir / "log"),
        "-o",
        options,
        "start",
    )


def _run(executable: Path, *args: str) -> None:
    """Run a bundled Postgres tool detached: no console, no inherited pipes.

    Without the new process group and ``DEVNULL`` streams, ``pg_ctl start`` leaves the
    postmaster holding this process's pipes and the call never returns.

    Raises:
        CommunityDbError: the tool could not run, timed out, or exited non-zero.
    """
    code = _exit_code(executable, *args)
    if code != 0:
        raise CommunityDbError(
            f"the bundled database tool {executable.name} failed "
            f"(exit {code}); see the log in the data folder",
            context={"reason": "pg_tool_failed", "tool": executable.name},
        )


def _exit_code(executable: Path, *args: str) -> int:
    """Run a bundled tool as :func:`_run` does and return its exit code.

    Raises:
        CommunityDbError: the tool could not run or timed out.
    """
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed bundled binary, no shell
            [str(executable), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            timeout=_TOOL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommunityDbError(
            f"the bundled database tool {executable.name} could not run",
            context={"reason": "pg_tool_failed", "tool": executable.name},
        ) from exc
    return completed.returncode


def _postmaster_listen_address(datadir: Path, bin_dir: Path) -> str | None:
    """The running postmaster's TCP listen address ("" when socket-only), or ``None``.

    Read from ``postmaster.pid`` (line 1, the PID; line 6, the listen address; line 8,
    the status). ``None`` when no postmaster is up and ready: the file is absent, still
    starting, or stale, because a crash left it behind and its PID is no longer our
    bundled ``postgres.exe`` (the PID may even belong to another program by now).
    """
    lines = _pid_file_lines(datadir)
    if lines is None or len(lines) < 8 or lines[7].strip() != "ready":
        return None
    pid = _pid_of(lines)
    if pid is None or not _live_postmaster(pid, bin_dir, datadir):
        return None
    return lines[5].strip()


def _pid_file_lines(datadir: Path) -> list[str] | None:
    try:
        return (datadir / "postmaster.pid").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None


def _pid_of(lines: list[str]) -> int | None:
    try:
        return int(lines[0].strip()) if lines else None
    except ValueError:
        return None


def _discard_stale_pid_file(datadir: Path, bin_dir: Path) -> None:
    """Remove a ``postmaster.pid`` whose PID is not a running bundled ``postgres.exe``.

    A crash leaves the file behind still saying "ready". On Windows ``pg_ctl -w``
    cannot compare PIDs, so it takes a pid file stamped within about two seconds of
    its own start as the new server's and returns before the new server has written
    its own; removing the stale file first makes it wait for the real one. A file
    whose PID is still our running server (even one still starting) is left alone.
    """
    lines = _pid_file_lines(datadir)
    if lines is None:
        return
    pid = _pid_of(lines)
    if pid is not None and _live_postmaster(pid, bin_dir, datadir):
        return
    delete_regular_file_nofollow(datadir / "postmaster.pid", root=datadir)


def _live_postmaster(pid: int, bin_dir: Path, datadir: Path) -> bool:
    """Whether ``pid`` is this data folder's running server (review I-1).

    Both must agree: the process is the bundled ``postgres.exe`` (a reused PID is some
    other program), and ``pg_ctl status -D <datadir>`` reports the server running.
    """
    return _postmaster_is_ours(pid, bin_dir) and _pg_ctl_says_running(bin_dir, datadir)


def _pg_ctl_says_running(bin_dir: Path, datadir: Path) -> bool:
    """``pg_ctl status`` exits 0 when the data folder's server is running (3 when not)."""
    return _exit_code(bin_dir / "pg_ctl.exe", "status", "-D", str(datadir)) == 0


def _postmaster_is_ours(pid: int, bin_dir: Path) -> bool:
    """Whether ``pid`` is a running ``postgres.exe`` from the bundled ``bin_dir``."""
    if sys.platform == "win32":
        from persona.tools._winacl import process_image

        image = process_image(pid)
        if image is None:
            return False
        expected = bin_dir / "postgres.exe"
        return os.path.normcase(os.path.realpath(image)) == os.path.normcase(
            os.path.realpath(expected)
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # another user's process: not ours
    return True


def _enforce_password_auth(datadir: Path, target: SocketTarget) -> None:
    """Make sure the superuser has the password and pg_hba.conf allows nothing else.

    A datadir that still trusts local connections (created by the bundled launcher or
    an older build) is upgraded: connect over the socket (trust ignores the password),
    set the password, write the scram-only ``pg_hba.conf`` and reload it. The password
    is sent as a SCRAM verifier computed here, as ``psql``'s ``\\password`` does, so the
    plain password never travels in the statement or reaches the server's logs.

    Raises:
        CommunityDbError: the upgrade connection or statements failed.
    """
    if _hba_requires_password(datadir):
        return
    import psycopg
    from psycopg import sql

    try:
        with psycopg.connect(
            host=str(target.socket_dir),
            port=target.port,
            user=_SUPERUSER,
            password=target.password,
            dbname="postgres",
            autocommit=True,
            connect_timeout=_CONNECT_TIMEOUT_S,
        ) as connection:
            verifier = connection.pgconn.encrypt_password(
                target.password.encode("utf-8"), _SUPERUSER.encode("ascii"), b"scram-sha-256"
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(_SUPERUSER), sql.Literal(verifier.decode("ascii"))
                )
            )
            (datadir / "pg_hba.conf").write_text(HBA_CONTENT, encoding="utf-8")
            connection.execute("SELECT pg_reload_conf()")
    except psycopg.Error as exc:
        raise CommunityDbError(
            "could not require a password on the bundled database",
            context={"reason": "auth_upgrade_failed", "path": str(datadir)},
        ) from exc
