"""The api marks itself non-dumpable on Linux (second security review F5).

The parse worker runs as the api's OS user; without this, a compromised worker could
read the api's environment and memory through ``/proc``. The reviewer's recon probe
read ``/proc/<api pid>/environ`` from the worker; these tests make that read from a
child process, before and after.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_LINUX = sys.platform.startswith("linux")

_READ_PARENT_ENVIRON = (
    "import os, sys\n"
    "try:\n"
    "    open(f'/proc/{os.getppid()}/environ', 'rb').read()\n"
    "except PermissionError:\n"
    "    sys.exit(3)\n"
)


def _child_reads_parent_environ(harden: bool) -> int:
    """Exit code of a child reading its parent's environment: 0 read, 3 refused.

    Root bypasses the dumpable check (CAP_SYS_PTRACE), so a probe started as root first
    becomes ``nobody``, as the api and worker are in production: an ordinary user.
    Changing user resets the flag, so the unhardened control sets it back to 1.
    """
    probe = (
        "import ctypes, os, subprocess, sys\n"
        "if os.geteuid() == 0:\n"
        "    os.setgid(65534)\n"
        "    os.setuid(65534)\n"
        "from persona_api.process_hardening import make_process_non_dumpable\n"
        f"if {harden!r}:\n"
        "    assert make_process_non_dumpable()\n"
        "else:\n"
        "    ctypes.CDLL(None).prctl(4, 1, 0, 0, 0)\n"
        f"sys.exit(subprocess.run([sys.executable, '-c', {_READ_PARENT_ENVIRON!r}]).returncode)\n"
    )
    return subprocess.run([sys.executable, "-c", probe], check=False).returncode


@pytest.mark.skipif(not _LINUX, reason="PR_SET_DUMPABLE is Linux only")
def test_a_same_user_child_cannot_read_a_hardened_apis_environment() -> None:
    assert _child_reads_parent_environ(harden=True) == 3


@pytest.mark.skipif(not _LINUX, reason="PR_SET_DUMPABLE is Linux only")
def test_without_hardening_the_same_child_can() -> None:
    """Precondition: the refusal above is the hardening, not something else."""
    assert _child_reads_parent_environ(harden=False) == 0


def test_elsewhere_it_says_it_did_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from persona_api import process_hardening  # noqa: PLC0415

    monkeypatch.setattr(process_hardening.sys, "platform", "darwin")
    assert process_hardening.make_process_non_dumpable() is False


def test_the_app_hardens_itself_first_thing_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from persona_api import app as app_module  # noqa: PLC0415
    from persona_api.config import APIConfig, Edition  # noqa: PLC0415

    class _HardenedError(Exception):
        pass

    def _record() -> bool:
        raise _HardenedError

    monkeypatch.setattr(app_module, "make_process_non_dumpable", _record)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "chroma",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
    )
    with pytest.raises(_HardenedError), TestClient(app_module.create_app(config)):
        pass
