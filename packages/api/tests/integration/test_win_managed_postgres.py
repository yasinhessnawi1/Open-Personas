"""The bundled Postgres on Windows, for real: socket only, scram, owner-only (Spec WIN T2).

Starts the Postgres bundled in pixeltable-pgserver through
:class:`CommunityDbManager` in a short temp folder and proves, against the running
server:

- no TCP listener: ``postmaster.pid`` has an empty listen address, and the
  postmaster process owns no listening TCP socket;
- the unix socket is inside the datadir, and the datadir and the password file carry
  only this account and SYSTEM, with nothing inherited from the parent folder;
- the superuser needs the password (none and a wrong one are both refused), and the
  connection string carries only the password file;
- the same Alembic chain migrates over that connection string, and a restart reuses
  the password;
- a datadir made the old way (the bundled launcher: loopback TCP with ``trust``) is
  upgraded on the next start: TCP stopped, the password set, ``pg_hba.conf`` replaced.

Windows only; marked ``integration`` (it provisions its own server).
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="the Windows lifecycle only"),
]

pytest.importorskip("pixeltable_pgserver")
psutil = pytest.importorskip("psutil")

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona_api.db.community_managed import CommunityDbManager


@pytest.fixture
def short_base_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # run_migrations sets os.environ["DATABASE_URL"] (D-K10-8); keep it out of other tests.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # A machine with another Postgres often exports libpq variables (the GitHub Windows
    # runner sets PGUSER=postgres and PGPASSWORD=root). PGPASSWORD is sent before the
    # password file is read; PGHOSTADDR would take the connection, and the password,
    # over TCP to 127.0.0.1:5432 instead of the socket; a PGSERVICE that does not exist
    # fails every connection. Every test runs with all of them set, so the lifecycle
    # must not depend on their absence.
    monkeypatch.setenv("PGPASSWORD", "another-postgres-password")
    monkeypatch.setenv("PGHOSTADDR", "127.0.0.1")
    monkeypatch.setenv("PGSERVICE", "persona-test-no-such-service")
    monkeypatch.setenv("PGUSER", "postgres")
    base = Path(tempfile.mkdtemp(prefix="wpg"))
    try:
        yield base
    finally:
        _stop_any(base / "pg16")
        shutil.rmtree(base, ignore_errors=True)


def _stop_any(datadir: Path) -> None:
    from persona_api.db.community_managed import _pginstall_bin_dir

    bin_dir = _pginstall_bin_dir()
    if bin_dir is not None and (datadir / "postmaster.pid").exists():
        subprocess.run(  # noqa: S603 - the bundled binary
            [str(bin_dir / "pg_ctl.exe"), "stop", "-D", str(datadir), "-m", "immediate"],
            capture_output=True,
            timeout=60,
            check=False,
        )


def _postmaster_pid(datadir: Path) -> int:
    return int((datadir / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0])


def _tcp_listeners(pid: int) -> list[object]:
    process = psutil.Process(pid)
    return [c for c in process.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN]


def _acl_principals(path: Path) -> tuple[list[str], bool]:
    """The principals on ``path``'s DACL and whether any entry is inherited."""
    listing = subprocess.run(  # noqa: S603 - icacls, a Windows builtin
        ["icacls", str(path)], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    entries = [line.replace(str(path), "").strip() for line in listing.splitlines()]
    entries = [e for e in entries if ":" in e and not e.startswith("Successfully")]
    return [e.split(":", 1)[0] for e in entries], any("(I)" in e for e in entries)


def _connect(manager: CommunityDbManager, **overrides: str) -> None:
    import psycopg

    params = {
        "host": str(manager._datadir),  # noqa: SLF001 - the socket folder under test
        "port": "5432",
        "user": "postgres",
        "dbname": "postgres",
        "connect_timeout": "10",
        **overrides,
    }
    with psycopg.connect(psycopg.conninfo.make_conninfo(**params)) as connection:
        connection.execute("SELECT 1")


def _assert_reached_over_the_socket(url: str) -> None:
    """The managed URL reaches the server over its unix socket, whatever PG* says.

    The server's own view: ``inet_client_addr()`` is NULL only for a unix-socket
    client, so a connection that PGHOSTADDR had sent over TCP would show an address.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT inet_client_addr()")).scalar() is None
    finally:
        engine.dispose()


def _assert_locked_down(manager: CommunityDbManager, datadir: Path) -> None:
    import psycopg

    listen = (datadir / "postmaster.pid").read_text(encoding="utf-8").splitlines()[5]
    assert listen.strip() == ""
    assert _tcp_listeners(_postmaster_pid(datadir)) == []
    assert (datadir / ".s.PGSQL.5432").exists()

    me = getpass.getuser().lower()
    for path in (datadir, datadir.with_name("pg16.pgpass")):
        principals, inherited = _acl_principals(path)
        assert not inherited, path
        assert sorted(p.split("\\")[-1].lower() for p in principals) == sorted([me, "system"])

    _assert_reached_over_the_socket(manager.database_url)
    with pytest.raises(psycopg.OperationalError):
        _connect(manager, passfile=str(datadir.with_name("absent.pgpass")))
    with pytest.raises(psycopg.OperationalError):
        _connect(manager, password="not-the-password")
    _connect(manager, passfile=str(datadir.with_name("pg16.pgpass")))


def test_a_fresh_install_is_socket_only_with_a_password_and_owner_only_files(
    short_base_dir: Path,
) -> None:
    from persona_api.db.community_managed import CommunityDbManager, run_migrations
    from sqlalchemy import create_engine, make_url, text

    datadir = short_base_dir / "pg16"
    manager = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    try:
        url = manager.start()
        password = (
            datadir.with_name("pg16.pgpass").read_text(encoding="utf-8").strip().split(":")[-1]
        )
        assert password not in url
        assert make_url(url).query["host"] == str(datadir)
        _assert_locked_down(manager, datadir)

        run_migrations(url)
        engine = create_engine(url)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar() == "persona"
        engine.dispose()
    finally:
        manager.stop()
    assert not (datadir / "postmaster.pid").exists()

    again = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    try:
        assert again.start() == url
        _assert_locked_down(again, datadir)
    finally:
        again.stop()


def _legacy_datadir_folder(datadir: Path) -> None:
    """The data folder as the bundled launcher found it under ``~/.persona``.

    A profile folder grants this account explicitly, beside entries inherited from its
    parent. ``tempfile.mkdtemp`` does not: its 0o700 folder grants SYSTEM, Administrators
    and OWNER RIGHTS only, and when an elevated administrator creates it (the GitHub
    runner) the owner is the Administrators group, so nothing names the account itself.
    ``initdb`` drops Administrators from its token before it touches the folder, could
    not open it, and failed ("could not create directory ...: File exists"). The
    account's own entry is added here, and the inherited ones stay, so the manager still
    has to restrict a folder that is not yet owner-only.
    """
    from persona.tools._winacl import current_user_sid

    datadir.mkdir()
    subprocess.run(  # noqa: S603 - icacls, a Windows builtin
        ["icacls", str(datadir), "/grant", f"*{current_user_sid()}:(OI)(CI)F"],
        capture_output=True,
        timeout=30,
        check=True,
    )


def test_a_trust_datadir_from_the_bundled_launcher_is_upgraded(short_base_dir: Path) -> None:
    import pixeltable_pgserver
    from persona_api.db._managed_windows import HBA_CONTENT
    from persona_api.db.community_managed import CommunityDbManager

    datadir = short_base_dir / "pg16"
    _legacy_datadir_folder(datadir)
    old = pixeltable_pgserver.get_server(datadir, cleanup_mode=None)  # type: ignore[attr-defined]
    old_pid = _postmaster_pid(datadir)
    assert _tcp_listeners(old_pid) != []  # the launcher's loopback TCP listener, with trust

    manager = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    try:
        manager.start()
        assert not psutil.pid_exists(old_pid) or old_pid != _postmaster_pid(datadir)
        assert (datadir / "pg_hba.conf").read_text(encoding="utf-8") == HBA_CONTENT
        _assert_locked_down(manager, datadir)
    finally:
        manager.stop()
        del old


def test_a_password_datadir_whose_password_file_is_gone_is_refused(
    short_base_dir: Path,
) -> None:
    from persona_api.db.community_managed import CommunityDbManager
    from persona_api.errors import CommunityDbError

    datadir = short_base_dir / "pg16"
    manager = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    try:
        manager.start()
    finally:
        manager.stop()
    datadir.with_name("pg16.pgpass").unlink()

    with pytest.raises(CommunityDbError) as caught:
        CommunityDbManager(external_url=None, base_dir=short_base_dir).start()
    assert caught.value.context["reason"] == "password_file_missing"


def test_a_postmaster_killed_by_a_crash_is_started_again(short_base_dir: Path) -> None:
    """Review L-1: a crash leaves postmaster.pid saying "ready"; it must not be trusted."""
    from persona_api.db.community_managed import CommunityDbManager

    datadir = short_base_dir / "pg16"
    first = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    first.start()
    old_pid = _postmaster_pid(datadir)
    tree = [psutil.Process(old_pid), *psutil.Process(old_pid).children(recursive=True)]
    for process in tree:
        process.kill()
    psutil.wait_procs(tree, timeout=30)
    assert (datadir / "postmaster.pid").exists()  # the stale file a crash leaves behind

    again = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    try:
        again.start()
        assert _postmaster_pid(datadir) != old_pid
        _assert_locked_down(again, datadir)
    finally:
        again.stop()


def test_a_base_folder_given_by_its_8_3_short_name_works(short_base_dir: Path) -> None:
    """Review L-3: the base is resolved once, so the short spelling reaches the same files."""
    import ctypes

    from persona_api.db.community_managed import CommunityDbManager

    long_base = short_base_dir / "a_rather_long_folder_name"
    long_base.mkdir()
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(long_base), buffer, 1024)  # type: ignore[attr-defined,unused-ignore]
    if buffer.value.lower() == str(long_base).lower():
        pytest.skip("8.3 short names are disabled on this volume")
    manager = CommunityDbManager(external_url=None, base_dir=Path(buffer.value))
    try:
        manager.start()
        _assert_locked_down(manager, long_base / "pg16")
    finally:
        manager.stop()
        _stop_any(long_base / "pg16")
