"""The api process makes itself non-dumpable on Linux (R9-258, D-ORCH-11-18-g).

A built-in MCP child runs as the api's own uid in the api's PID namespace, so
without this it could read every api secret from ``/proc/<api pid>/environ``.
These tests inject a fake ``prctl``; the real kernel behaviour is pinned by
``tests/integration/test_process_hardening_procfs.py`` (Linux only).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import uvicorn
from persona_api import process_hardening
from persona_api.process_hardening import PR_SET_DUMPABLE, make_non_dumpable


class _Prctl:
    def __init__(self, result: int = 0, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[int, ...]] = []
        self._result = result
        self._raises = raises

    def __call__(self, *args: int) -> int:
        self.calls.append(args)
        if self._raises is not None:
            raise self._raises
        return self._result


def test_pr_set_dumpable_is_the_kernel_constant() -> None:
    # include/uapi/linux/prctl.h: #define PR_SET_DUMPABLE 4
    assert PR_SET_DUMPABLE == 4


@pytest.mark.parametrize("platform", ["linux", "linux2"])
def test_linux_calls_prctl_set_dumpable_zero(platform: str) -> None:
    prctl = _Prctl()
    assert make_non_dumpable(platform=platform, prctl=prctl) is True
    assert prctl.calls == [(4, 0, 0, 0, 0)]


@pytest.mark.parametrize("platform", ["win32", "darwin", "cygwin", "freebsd14"])
def test_other_platforms_do_nothing(platform: str) -> None:
    prctl = _Prctl()
    assert make_non_dumpable(platform=platform, prctl=prctl) is False
    assert prctl.calls == []


def test_a_failing_prctl_is_reported_not_raised() -> None:
    warnings: list[str] = []
    prctl = _Prctl(result=-1)
    assert make_non_dumpable(platform="linux", prctl=prctl, warn=warnings.append) is False
    assert prctl.calls == [(4, 0, 0, 0, 0)]
    assert any("dumpable" in w for w in warnings)


@pytest.mark.parametrize("error", [OSError("no libc"), AttributeError("no prctl")])
def test_a_missing_prctl_is_reported_not_raised(error: BaseException) -> None:
    warnings: list[str] = []
    prctl = _Prctl(raises=error)
    assert make_non_dumpable(platform="linux", prctl=prctl, warn=warnings.append) is False
    assert warnings


def test_the_app_factory_hardens_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # create_app runs in every process uvicorn serves from (with --workers 1 that is
    # PID 1 itself; spawned workers each run it too), so the hook lives there.
    from persona_api import app as app_module

    calls: list[str] = []

    def _hook() -> bool:
        calls.append("hook")
        return True

    monkeypatch.setattr(app_module, "make_non_dumpable", _hook)

    class _StopError(Exception):
        pass

    def _stop(_config: object) -> None:
        raise _StopError

    monkeypatch.setattr(app_module, "check_public_noauth_guard", _stop)
    with pytest.raises(_StopError):
        app_module.create_app(config=object())  # type: ignore[arg-type]
    assert calls == ["hook"], "create_app must harden the process before anything else"


def test_the_community_entrypoint_hardens_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    from persona_api import __main__ as entry

    calls: list[str] = []

    def _hook() -> bool:
        calls.append("hook")
        return True

    monkeypatch.setattr(entry, "make_non_dumpable", _hook)
    monkeypatch.setattr(entry, "_load_env_file", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: calls.append("run"))
    entry.main()
    assert calls == ["hook", "run"]


def test_the_image_serves_from_one_process() -> None:
    # The hook runs in the app factory. uvicorn with --workers 1 serves from PID 1
    # itself, so that process is hardened. With more workers the uvicorn master
    # (which never runs the factory) would keep every secret and stay dumpable:
    # raising the worker count needs the hook in the entrypoint first.
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    cmd = re.search(r"^CMD \[(.*?)\]", dockerfile, re.M | re.S)
    assert cmd is not None
    assert '"uvicorn"' in cmd.group(1)
    assert re.search(r'"--workers",\s*"1"', cmd.group(1)), "more workers need an entrypoint hook"


def test_module_imports_nothing_heavy() -> None:
    # Imported first thing by create_app; it must stay standard library only.
    source = Path(process_hardening.__file__).read_text()
    imports = re.findall(r"^(?:from|import) (\S+)", source, re.M)
    assert set(imports) <= {"__future__", "ctypes", "sys", "typing", "collections.abc"}
