"""The Windows lifecycle of the bundled Postgres, without a real server (Spec WIN T2).

The real start (socket only, scram, the owner-only folder, the trust upgrade) is
proved by ``tests/integration/test_win_managed_postgres.py`` on Windows. These tests
pin the decisions around it on every platform: the connection string, the password
file, the order of the tool calls, and the refusals.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from persona_api.db import _managed_windows as mw
from persona_api.db import community_managed as cm
from persona_api.db._managed_files import create_private_file
from persona_api.errors import CommunityDbError
from sqlalchemy.engine import make_url

WINDOWS_DATADIR = Path("C:\\Users\\someone\\.persona\\pg16")


def _target(datadir: Path = WINDOWS_DATADIR) -> mw.SocketTarget:
    return mw.SocketTarget(
        socket_dir=datadir,
        port=mw.SOCKET_PORT,
        password_file=mw.password_file_for(datadir),
        password="s3cret-Value_1",
    )


class TestConnectionString:
    def test_a_windows_socket_folder_survives_the_url_round_trip(self) -> None:
        url = make_url(_target().sqlalchemy_url("persona"))
        assert url.drivername == "postgresql+psycopg"
        assert url.username == "postgres"
        assert url.database == "persona"
        assert url.host is None  # the socket folder travels as a query parameter
        assert url.query["host"] == "C:\\Users\\someone\\.persona\\pg16"
        assert url.query["port"] == "5432"
        assert url.query["passfile"] == "C:\\Users\\someone\\.persona\\pg16.pgpass"

    def test_the_url_pins_what_the_environment_could_otherwise_change(self) -> None:
        """scram only (no cleartext or md5 password to a server that asks for one), no
        GSS encryption attempt, and any server role, whatever PG* variables say."""
        query = make_url(_target().sqlalchemy_url("persona")).query
        assert query["require_auth"] == "scram-sha-256"
        assert query["target_session_attrs"] == "any"
        assert query["gssencmode"] == "disable"

    def test_the_url_names_the_password_file_never_the_password(self) -> None:
        rendered = _target().sqlalchemy_url("persona")
        assert "s3cret-Value_1" not in rendered
        assert make_url(rendered).password is None

    def test_the_password_is_not_in_the_repr(self) -> None:
        assert "s3cret-Value_1" not in repr(_target())

    def test_the_password_file_sits_beside_the_datadir(self) -> None:
        assert mw.password_file_for(Path("/home/u/.persona/pg16")) == Path(
            "/home/u/.persona/pg16.pgpass"
        )


class TestPasswordFile:
    def test_a_fresh_install_writes_one_libpq_line(self, tmp_path: Path) -> None:
        datadir = tmp_path / "pg16"
        password = mw._ensure_password(mw.password_file_for(datadir), datadir, fresh=True)
        assert len(password) >= 40
        content = mw.password_file_for(datadir).read_text(encoding="utf-8")
        assert content == f"*:*:*:postgres:{password}\n"

    def test_the_stored_password_is_reused(self, tmp_path: Path) -> None:
        datadir = tmp_path / "pg16"
        first = mw._ensure_password(mw.password_file_for(datadir), datadir, fresh=True)
        again = mw._ensure_password(mw.password_file_for(datadir), datadir, fresh=False)
        assert again == first

    def test_a_password_datadir_without_its_file_is_refused(self, tmp_path: Path) -> None:
        datadir = tmp_path / "pg16"
        datadir.mkdir()
        (datadir / "pg_hba.conf").write_text(mw.HBA_CONTENT, encoding="utf-8")
        with pytest.raises(CommunityDbError) as caught:
            mw._ensure_password(mw.password_file_for(datadir), datadir, fresh=False)
        assert caught.value.context["reason"] == "password_file_missing"
        assert not mw.password_file_for(datadir).exists()

    def test_a_trust_datadir_without_a_file_gets_a_new_password(self, tmp_path: Path) -> None:
        datadir = tmp_path / "pg16"
        datadir.mkdir()
        (datadir / "pg_hba.conf").write_text("local all all trust\n", encoding="utf-8")
        password = mw._ensure_password(mw.password_file_for(datadir), datadir, fresh=False)
        assert mw._stored_password(mw.password_file_for(datadir)) == password

    def test_a_file_of_ours_without_the_superuser_line_is_refused(self, tmp_path: Path) -> None:
        """Never silently replaced: a scram datadir may still need the password it held."""
        passfile = tmp_path / "pg16.pgpass"
        create_private_file(passfile, b"*:*:*:someone_else:pw\n")
        with pytest.raises(CommunityDbError) as caught:
            mw._stored_password(passfile)
        assert caught.value.context["reason"] == "password_file_unreadable"

    def test_a_password_file_someone_else_made_is_refused(self, tmp_path: Path) -> None:
        """Review M-1: a planted pg16.pgpass is never read or reused.

        ``write_text`` makes a file the way anyone else would: on Windows it inherits the
        folder's entries (Administrators among them), on POSIX it is group and world
        readable. The product only ever creates it owner-only.
        """
        datadir = tmp_path / "pg16"
        passfile = mw.password_file_for(datadir)
        passfile.write_text("*:*:*:postgres:planted-by-someone\n", encoding="utf-8")
        passfile.chmod(0o644)
        with pytest.raises(CommunityDbError) as caught:
            mw._ensure_password(passfile, datadir, fresh=True)
        assert caught.value.context["reason"] == "unsafe_private_file"
        assert str(passfile) in str(caught.value)

    def test_the_password_file_is_never_created_over_an_existing_one(self, tmp_path: Path) -> None:
        passfile = tmp_path / "pg16.pgpass"
        create_private_file(passfile, b"first\n")
        with pytest.raises(CommunityDbError) as caught:
            create_private_file(passfile, b"second\n")
        assert caught.value.context["reason"] == "private_file_exists"
        assert passfile.read_bytes() == b"first\n"

    def test_a_linked_password_file_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        target.write_text("*:*:*:postgres:planted\n", encoding="utf-8")
        link = tmp_path / "pg16.pgpass"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("this account cannot create symlinks")
        with pytest.raises(OSError, match="(?i)link") as caught:
            mw._stored_password(link)
        assert caught.value.errno == errno.ELOOP


def _pid_file(datadir: Path, *, listen: str, status: str = "ready") -> None:
    datadir.mkdir(parents=True, exist_ok=True)
    lines = ["4242", str(datadir), "1700000000", "5432", str(datadir), listen, "0", status]
    (datadir / "postmaster.pid").write_text("\n".join(lines) + "\n", encoding="utf-8")


BIN = Path("bin")


@pytest.fixture
def live_postmaster(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every PID counts as the bundled postgres.exe; records the PIDs asked about."""
    asked: list[int] = []

    def ours(pid: int, _bin_dir: Path) -> bool:
        asked.append(pid)
        return True

    monkeypatch.setattr(mw, "_postmaster_is_ours", ours)
    monkeypatch.setattr(mw, "_pg_ctl_says_running", lambda _bin, _datadir: True)
    return asked


class TestPostmasterPid:
    def test_no_pid_file_means_no_server(self, tmp_path: Path) -> None:
        assert mw._postmaster_listen_address(tmp_path, BIN) is None

    def test_a_server_still_starting_is_not_counted(
        self, tmp_path: Path, live_postmaster: list[int]
    ) -> None:
        _pid_file(tmp_path, listen="", status="starting")
        assert mw._postmaster_listen_address(tmp_path, BIN) is None
        assert live_postmaster == []  # not ready: the PID is not even looked at

    @pytest.mark.parametrize("listen", ["", "127.0.0.1", "*"])
    def test_the_listen_address_is_line_six(
        self, tmp_path: Path, listen: str, live_postmaster: list[int]
    ) -> None:
        _pid_file(tmp_path, listen=listen)
        assert mw._postmaster_listen_address(tmp_path, BIN) == listen
        assert live_postmaster == [4242]  # line 1 is the PID that was checked

    def test_a_stale_pid_file_left_by_a_crash_is_not_a_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review L-1: the file still says ready, but its PID is no longer our postgres."""
        monkeypatch.setattr(mw, "_postmaster_is_ours", lambda _pid, _bin: False)
        _pid_file(tmp_path, listen="")
        assert mw._postmaster_listen_address(tmp_path, BIN) is None

    def test_a_garbled_pid_is_not_a_server(
        self, tmp_path: Path, live_postmaster: list[int]
    ) -> None:
        _pid_file(tmp_path, listen="")
        pid_file = tmp_path / "postmaster.pid"
        pid_file.write_text("x" + pid_file.read_text(encoding="utf-8"), encoding="utf-8")
        assert mw._postmaster_listen_address(tmp_path, BIN) is None
        assert live_postmaster == []

    def test_this_process_is_not_the_bundled_postgres(self, tmp_path: Path) -> None:
        import os

        assert mw._postmaster_is_ours(os.getpid(), tmp_path) is (sys.platform != "win32")
        assert mw._postmaster_is_ours(2**22 + 7, tmp_path) is False  # no such process


class _Tools:
    """Records the bundled-tool calls and plays the server's side of them."""

    def __init__(self, datadir: Path, *, listen_after_start: str = "") -> None:
        self.datadir = datadir
        self.listen_after_start = listen_after_start
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.pwfile_content: str | None = None

    def __call__(self, executable: Path, *args: str) -> None:
        self.calls.append((executable.name, args))
        if executable.name == "initdb.exe":
            pwfile = Path(next(a for a in args if a.startswith("--pwfile=")).split("=", 1)[1])
            self.pwfile_content = pwfile.read_text(encoding="utf-8")
            self.datadir.mkdir(parents=True, exist_ok=True)
            (self.datadir / "PG_VERSION").write_text("16\n", encoding="utf-8")
        elif args[-1] == "start":
            _pid_file(self.datadir, listen=self.listen_after_start)
        elif args[0] == "stop":
            (self.datadir / "postmaster.pid").unlink()


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Tools:
    recorder = _Tools(tmp_path / "pg16")
    monkeypatch.setattr(mw, "_run", recorder)
    monkeypatch.setattr(mw, "_enforce_password_auth", lambda _d, _t: None)
    monkeypatch.setattr(mw, "_postmaster_is_ours", lambda _pid, _bin: True)
    monkeypatch.setattr(mw, "_pg_ctl_says_running", lambda _bin, _datadir: True)
    return recorder


class TestStartSocketOnly:
    def test_a_fresh_datadir_is_created_with_scram_and_started_without_tcp(
        self, tmp_path: Path, tools: _Tools
    ) -> None:
        datadir = tmp_path / "pg16"
        target = mw.start_socket_only(datadir, tmp_path / "bin")
        names = [name for name, _ in tools.calls]
        assert names == ["initdb.exe", "pg_ctl.exe"]
        initdb_args = tools.calls[0][1]
        assert "--auth-local=scram-sha-256" in initdb_args
        assert "--auth-host=scram-sha-256" in initdb_args
        assert not any("trust" in arg for arg in initdb_args)
        assert tools.pwfile_content == f"{target.password}\n"
        assert not datadir.with_name("pg16.initpw").exists()  # staged only for initdb
        assert (datadir / "pg_hba.conf").read_text(encoding="utf-8") == mw.HBA_CONTENT
        start_args = tools.calls[1][1]
        options = start_args[start_args.index("-o") + 1]
        assert options == f'-h "" -k "{datadir}" -p 5432'
        assert target.socket_dir == datadir
        assert target.password_file == datadir.with_name("pg16.pgpass")

    def test_the_hba_file_has_no_host_line_and_no_trust(self) -> None:
        rules = [
            line.split()
            for line in mw.HBA_CONTENT.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert rules == [["local", "all", "all", "scram-sha-256"]]

    def test_a_tcp_postmaster_is_stopped_before_the_socket_only_start(
        self, tmp_path: Path, tools: _Tools
    ) -> None:
        datadir = tmp_path / "pg16"
        _pid_file(datadir, listen="127.0.0.1")
        (datadir / "PG_VERSION").write_text("16\n", encoding="utf-8")
        mw.start_socket_only(datadir, tmp_path / "bin")
        assert [(name, args[0]) for name, args in tools.calls] == [
            ("pg_ctl.exe", "stop"),
            ("pg_ctl.exe", "-w"),
        ]

    def test_a_running_socket_only_postmaster_is_reused(
        self, tmp_path: Path, tools: _Tools
    ) -> None:
        datadir = tmp_path / "pg16"
        _pid_file(datadir, listen="")
        (datadir / "PG_VERSION").write_text("16\n", encoding="utf-8")
        mw.start_socket_only(datadir, tmp_path / "bin")
        assert tools.calls == []

    def test_a_server_that_comes_up_on_tcp_is_refused(self, tmp_path: Path, tools: _Tools) -> None:
        tools.listen_after_start = "127.0.0.1"
        with pytest.raises(CommunityDbError) as caught:
            mw.start_socket_only(tmp_path / "pg16", tmp_path / "bin")
        assert caught.value.context["reason"] == "not_socket_only"

    def test_the_staged_initdb_password_is_removed_when_initdb_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing(_executable: Path, *_args: str) -> None:
            raise CommunityDbError("boom", context={"reason": "pg_tool_failed"})

        monkeypatch.setattr(mw, "_run", failing)
        with pytest.raises(CommunityDbError):
            mw.start_socket_only(tmp_path / "pg16", tmp_path / "bin")
        assert not (tmp_path / "pg16.initpw").exists()


class TestTheUpgradeIsSkippedOnlyForTheScramHba:
    def test_a_scram_datadir_is_not_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pg_hba.conf").write_text(mw.HBA_CONTENT, encoding="utf-8")
        import psycopg

        def no_connect(*_a: object, **_k: object) -> None:
            raise AssertionError("a scram datadir needs no upgrade connection")

        monkeypatch.setattr(psycopg, "connect", no_connect)
        mw._enforce_password_auth(tmp_path, _target(tmp_path))

    def test_a_failed_upgrade_is_a_community_db_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pg_hba.conf").write_text("local all all trust\n", encoding="utf-8")
        import psycopg

        def refused(*_a: object, **_k: object) -> None:
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(psycopg, "connect", refused)
        with pytest.raises(CommunityDbError) as caught:
            mw._enforce_password_auth(tmp_path, _target(tmp_path))
        assert caught.value.context["reason"] == "auth_upgrade_failed"
        assert "trust" in (tmp_path / "pg_hba.conf").read_text(encoding="utf-8")


@pytest.fixture
def short_base() -> Iterator[Path]:
    """A base folder short enough for the socket path guard (pytest's tmp_path is not)."""
    base = Path(tempfile.mkdtemp(prefix="w"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def logged_warnings() -> Iterator[list[str]]:
    """Every WARNING and above, as logged (persona.logging wraps loguru, so stdlib
    ``caplog`` sees nothing; the test_api_boot_hermetic.py pattern)."""
    from loguru import logger

    captured: list[str] = []
    sink = logger.add(lambda message: captured.append(str(message)), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink)


class TestTheManagerRouting:
    def test_pg_ctl_has_the_exe_suffix_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert cm._executable_name("pg_ctl") == "pg_ctl.exe"
        monkeypatch.setattr(sys, "platform", "linux")
        assert cm._executable_name("pg_ctl") == "pg_ctl"

    def test_windows_uses_the_socket_only_lifecycle(
        self, short_base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[Path] = []
        admin_urls: list[str] = []

        def fake_start(datadir: Path, _bin_dir: Path) -> mw.SocketTarget:
            started.append(datadir)
            return _target(datadir)

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(mw, "start_socket_only", fake_start)
        monkeypatch.setattr(cm, "_pginstall_bin_dir", lambda: short_base / "bin")
        monkeypatch.setattr(cm.CommunityDbManager, "_ensure_database", _recorder(admin_urls))
        manager = cm.CommunityDbManager(external_url=None, base_dir=short_base)
        url = manager.start()
        # On Windows the manager resolves its base folder once (review L-3), so the datadir
        # is the long spelling even when the temp folder is reached through an 8.3 short
        # name (the GitHub runner's TEMP is C:\Users\RUNNER~1\...).
        assert started == [short_base.resolve() / "pg16"]
        assert make_url(admin_urls[0]).database == "postgres"
        assert make_url(url).database == "persona"
        assert str(make_url(url).query["passfile"]).endswith("pg16.pgpass")

    def test_windows_without_the_bundled_binaries_is_refused(
        self, short_base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(cm, "_pginstall_bin_dir", lambda: None)
        manager = cm.CommunityDbManager(external_url=None, base_dir=short_base)
        with pytest.raises(CommunityDbError) as caught:
            manager.start()
        assert caught.value.context["reason"] == "embedded_package_missing"

    def test_the_socket_path_guard_still_applies_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        long_base = tmp_path / ("x" * 120)
        manager = cm.CommunityDbManager(external_url=None, base_dir=long_base)
        with pytest.raises(CommunityDbError) as caught:
            manager.start()
        assert caught.value.context["reason"] == "socket_path_too_long"

    def test_libpq_environment_overrides_are_dropped_before_the_first_connection(
        self,
        short_base: Path,
        monkeypatch: pytest.MonkeyPatch,
        logged_warnings: list[str],
    ) -> None:
        """libpq reads these from the environment, and each one takes the connection
        somewhere its string does not say: PGPASSWORD is sent before the ``passfile`` is
        read (the GitHub Windows runner sets PGPASSWORD=root); PGHOSTADDR sends it over
        TCP to that address instead of the socket, with the password file's line;
        PGSERVICE and PGSERVICEFILE pull parameters from a service file. The Windows
        lifecycle drops them all before its first connection and names them, never
        their values, in one warning.
        """
        names = ("PGPASSWORD", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
        seen_at_start: list[dict[str, str | None]] = []
        seen_at_connect: list[dict[str, str | None]] = []

        def snapshot() -> dict[str, str | None]:
            return {name: os.environ.get(name) for name in names}

        def fake_start(datadir: Path, _bin_dir: Path) -> mw.SocketTarget:
            seen_at_start.append(snapshot())
            return _target(datadir)

        def fake_ensure(_self: object, _admin_url: str) -> None:
            seen_at_connect.append(snapshot())

        values = {name: f"value-of-{name.lower()}" for name in names}
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("PGUSER", "postgres")  # harmless: the URL names the user
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(mw, "start_socket_only", fake_start)
        monkeypatch.setattr(cm, "_pginstall_bin_dir", lambda: short_base / "bin")
        monkeypatch.setattr(cm.CommunityDbManager, "_ensure_database", fake_ensure)
        cm.CommunityDbManager(external_url=None, base_dir=short_base).start()
        nothing = dict.fromkeys(names)
        assert seen_at_start == [nothing]
        assert seen_at_connect == [nothing]
        assert os.environ.get("PGUSER") == "postgres"
        warning = " ".join(logged_warnings)
        assert all(name in warning for name in names), warning
        assert not any(value in warning for value in values.values()), warning
        assert "child processes" in warning, warning

    def test_nothing_is_logged_when_no_override_is_set(
        self,
        short_base: Path,
        monkeypatch: pytest.MonkeyPatch,
        logged_warnings: list[str],
    ) -> None:
        for name in mw.ENVIRONMENT_OVERRIDES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(mw, "start_socket_only", lambda datadir, _bin: _target(datadir))
        monkeypatch.setattr(cm, "_pginstall_bin_dir", lambda: short_base / "bin")
        monkeypatch.setattr(cm.CommunityDbManager, "_ensure_database", _recorder([]))
        cm.CommunityDbManager(external_url=None, base_dir=short_base).start()
        assert not [line for line in logged_warnings if "environment" in line]

    def test_the_posix_lifecycle_leaves_pgpassword_alone(
        self, short_base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the Windows lifecycle authenticates by password file; elsewhere the
        embedded server trusts its 0700 socket, so the environment is not touched."""

        class _Server:
            def ensure_postgres_running(self) -> None:
                return None

            def get_uri(self, database: str) -> str:
                return f"postgresql://postgres@/{database}?host={short_base}"

        import pixeltable_pgserver

        monkeypatch.setenv("PGPASSWORD", "root")
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            pixeltable_pgserver, "get_server", lambda *_a, **_k: _Server(), raising=False
        )
        monkeypatch.setattr(cm.CommunityDbManager, "_ensure_database", _recorder([]))
        cm.CommunityDbManager(external_url=None, base_dir=short_base).start()
        assert os.environ.get("PGPASSWORD") == "root"


def _recorder(sink: list[str]) -> Callable[[object, str], None]:
    def record(_self: object, admin_url: str) -> None:
        sink.append(admin_url)

    return record


def test_a_percent_encoded_url_reaches_alembic_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """configparser interpolation treats "%" as special; the DSN must arrive unchanged."""
    from alembic import command

    seen: list[str | None] = []
    monkeypatch.setattr(
        command, "upgrade", lambda cfg, _rev: seen.append(cfg.get_main_option("sqlalchemy.url"))
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = _target().sqlalchemy_url("persona")
    assert "%5C" in url
    try:
        cm.run_migrations(url)
    finally:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    assert seen == [url]


class TestTheUpgradeSendsAVerifier:
    """Review L-2: the upgrade sends a SCRAM verifier, never the plain password."""

    def test_alter_role_carries_the_client_computed_verifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import psycopg

        (tmp_path / "pg_hba.conf").write_text("local all all trust\n", encoding="utf-8")
        verifier = b"SCRAM-SHA-256$4096:c2FsdA==$c3RvcmVk:c2VydmVy"
        asked: list[tuple[bytes, bytes, bytes]] = []
        statements: list[str] = []

        class _PgConn:
            def encrypt_password(self, password: bytes, user: bytes, algorithm: bytes) -> bytes:
                asked.append((password, user, algorithm))
                return verifier

        class _Connection:
            pgconn = _PgConn()

            def __enter__(self) -> _Connection:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def execute(self, statement: object) -> None:
                text = statement if isinstance(statement, str) else statement.as_string(None)  # type: ignore[attr-defined]
                statements.append(text)

        monkeypatch.setattr(psycopg, "connect", lambda **_kw: _Connection())
        mw._enforce_password_auth(tmp_path, _target(tmp_path))

        assert asked == [(b"s3cret-Value_1", b"postgres", b"scram-sha-256")]
        alter = [s for s in statements if s.startswith("ALTER ROLE")]
        assert alter == [f"ALTER ROLE \"postgres\" PASSWORD '{verifier.decode()}'"]
        assert not any("s3cret-Value_1" in s for s in statements)
        assert (tmp_path / "pg_hba.conf").read_text(encoding="utf-8") == mw.HBA_CONTENT


class TestTheStagedInitdbPassword:
    def test_a_leftover_of_ours_from_a_crash_is_replaced(
        self, tmp_path: Path, tools: _Tools
    ) -> None:
        staged = tmp_path / "pg16.initpw"
        create_private_file(staged, b"old-password\n")
        target = mw.start_socket_only(tmp_path / "pg16", tmp_path / "bin")
        assert tools.pwfile_content == f"{target.password}\n"
        assert not staged.exists()

    def test_a_staged_file_someone_else_made_is_refused(
        self, tmp_path: Path, tools: _Tools
    ) -> None:
        staged = tmp_path / "pg16.initpw"
        staged.write_text("planted\n", encoding="utf-8")
        staged.chmod(0o644)
        with pytest.raises(CommunityDbError) as caught:
            mw.start_socket_only(tmp_path / "pg16", tmp_path / "bin")
        assert caught.value.context["reason"] == "unsafe_private_file"
        assert tools.calls == []  # initdb never ran
        assert staged.read_text(encoding="utf-8") == "planted\n"


class TestAStalePidFileIsRemovedBeforeTheStart:
    """Review L-1: pg_ctl on Windows would take the stale file for the new server's."""

    def test_a_crash_leftover_is_removed_so_the_start_waits_for_the_new_server(
        self, tmp_path: Path, tools: _Tools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datadir = tmp_path / "pg16"
        _pid_file(datadir, listen="")
        (datadir / "PG_VERSION").write_text("16\n", encoding="utf-8")
        stale = (datadir / "postmaster.pid").read_text(encoding="utf-8").replace("4242", "1111")
        (datadir / "postmaster.pid").write_text(stale, encoding="utf-8")
        monkeypatch.setattr(mw, "_postmaster_is_ours", lambda pid, _bin: pid == 4242)
        seen_at_start: list[bool] = []
        real_call = tools.__call__

        def recording(executable: Path, *args: str) -> None:
            if args and args[-1] == "start":
                seen_at_start.append((datadir / "postmaster.pid").exists())
            real_call(executable, *args)

        monkeypatch.setattr(mw, "_run", recording)
        mw.start_socket_only(datadir, tmp_path / "bin")
        assert seen_at_start == [False]  # the stale file was gone when pg_ctl started

    def test_the_file_of_a_server_still_starting_is_left_alone(
        self, tmp_path: Path, live_postmaster: list[int]
    ) -> None:
        _pid_file(tmp_path, listen="", status="starting")
        mw._discard_stale_pid_file(tmp_path, BIN)
        assert (tmp_path / "postmaster.pid").exists()
        assert live_postmaster == [4242]

    def test_no_file_is_fine(self, tmp_path: Path) -> None:
        mw._discard_stale_pid_file(tmp_path, BIN)


class TestALivePostmasterNeedsPgCtlToo:
    """Review I-1: the image name alone does not identify this data folder's server."""

    def test_our_image_but_pg_ctl_says_not_running_is_not_a_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mw, "_postmaster_is_ours", lambda _pid, _bin: True)
        monkeypatch.setattr(mw, "_pg_ctl_says_running", lambda _bin, _datadir: False)
        _pid_file(tmp_path, listen="")
        assert mw._postmaster_listen_address(tmp_path, BIN) is None
        mw._discard_stale_pid_file(tmp_path, BIN)
        assert not (tmp_path / "postmaster.pid").exists()

    def test_pg_ctl_running_but_not_our_image_is_not_a_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mw, "_postmaster_is_ours", lambda _pid, _bin: False)
        monkeypatch.setattr(mw, "_pg_ctl_says_running", lambda _bin, _datadir: True)
        _pid_file(tmp_path, listen="")
        assert mw._postmaster_listen_address(tmp_path, BIN) is None

    @pytest.mark.parametrize(("code", "running"), [(0, True), (3, False), (4, False)])
    def test_pg_ctl_status_is_asked_about_this_data_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, running: bool
    ) -> None:
        asked: list[tuple[str, tuple[str, ...]]] = []

        def exit_code(executable: Path, *args: str) -> int:
            asked.append((executable.name, args))
            return code

        monkeypatch.setattr(mw, "_exit_code", exit_code)
        assert mw._pg_ctl_says_running(BIN, tmp_path) is running
        assert asked == [("pg_ctl.exe", ("status", "-D", str(tmp_path)))]


class TestTheFoldersStayHeldUntilTheServerIsUp:
    """Review N-1: the start runs inside the held-folders block, never after it."""

    def test_initdb_and_the_start_run_while_the_folders_are_held(
        self, tmp_path: Path, tools: _Tools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import contextmanager

        held: list[bool] = [False]
        seen: list[tuple[str, bool]] = []
        from persona_api.db._managed_files import prepared_folders as real_prepared

        @contextmanager
        def recording(datadir: Path) -> Iterator[None]:
            with real_prepared(datadir):
                held[0] = True
                try:
                    yield
                finally:
                    held[0] = False

        real_call = tools.__call__

        def run(executable: Path, *args: str) -> None:
            seen.append((args[-1] if args else executable.name, held[0]))
            real_call(executable, *args)

        monkeypatch.setattr(mw, "prepared_folders", recording)
        monkeypatch.setattr(mw, "_run", run)
        mw.start_socket_only(tmp_path / "pg16", tmp_path / "bin")
        assert [step for step, _ in seen] == [seen[0][0], "start"]
        assert all(was_held for _, was_held in seen)
        assert held == [False]  # released once the server is up
