"""A non-dumpable api process hides its env from its own children (R9-258, Linux).

Production runs the api as one non-root uid, and a built-in MCP child runs as that
same uid in the same PID namespace. A same-uid process may read another's
``/proc/<pid>/environ`` unless the target is non-dumpable. So this spawns a real
parent that holds a sentinel secret, lets it spawn a real child with a clean env,
and has the child try to read ``/proc/<parent pid>/environ``:

- the parent hardened with :func:`make_non_dumpable`: the read is refused;
- the control, a parent left dumpable: the same read returns the sentinel, so the
  first case cannot pass because the read path itself is broken.

Linux only; marked ``integration`` so CI's ubuntu job runs it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux procfs only"),
]

_SENTINEL = "r9-258-procfs-sentinel"
_API_SRC = Path(__file__).resolve().parents[2] / "src"

#: The child: read the parent's environ; print READ plus whether the sentinel was there,
#: or DENIED plus the errno name.
_CHILD = f"""
import errno, os
try:
    with open(f"/proc/{{os.getppid()}}/environ", "rb") as fh:
        data = fh.read()
except OSError as exc:
    print("DENIED", errno.errorcode.get(exc.errno, exc.errno))
else:
    print("READ", {_SENTINEL!r}.encode() in data)
"""

#: The parent: hold the sentinel, optionally harden, then spawn the child with a
#: clean env (as the launcher does) and relay its answer.
_PARENT = """
import os, subprocess, sys
harden = sys.argv[1] == "harden"
if harden:
    from persona_api.process_hardening import make_non_dumpable
    if not make_non_dumpable():
        sys.exit("make_non_dumpable failed")
out = subprocess.run(
    [sys.executable, "-c", sys.argv[2]],
    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    capture_output=True, text=True, check=True,
)
print(out.stdout.strip())
"""


def _is_root() -> bool:
    # Root (CAP_SYS_PTRACE) reads any procfs environ, which would void both tests.
    return sys.platform.startswith("linux") and os.geteuid() == 0


def _child_answer(mode: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(_API_SRC),
        "STRIPE_SECRET_KEY": _SENTINEL,
    }
    done = subprocess.run(  # noqa: S603 (fixed argv: this interpreter and inline scripts)
        [sys.executable, "-c", _PARENT, mode, _CHILD],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_a_dumpable_parent_leaks_its_env_to_a_same_uid_child() -> None:
    # The control. If this fails the read path is broken and the next test means nothing.
    if _is_root():
        pytest.fail("run as a non-root user: root reads any procfs environ")
    assert _child_answer("plain") == "READ True"


def test_a_non_dumpable_parent_refuses_the_child() -> None:
    if _is_root():
        pytest.fail("run as a non-root user: root reads any procfs environ")
    assert _child_answer("harden") == "DENIED EACCES"
