"""Keep the api's environment out of reach of its own children (R9-258, D-ORCH-11-18-g).

In production the api runs as one non-root uid, and the built-in MCP children run
as that same uid in the same PID namespace (the api is PID 1 of its container). A
child starts with an allow-listed env, but a same-uid process may still read
another's ``/proc/<pid>/environ``, so a compromised child could read every api
secret from ``/proc/$PPID/environ`` or ``/proc/1/environ``.

Marking the api process non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``) makes the
kernel refuse those reads to anything without ``CAP_SYS_PTRACE``. The cost,
accepted in D-ORCH-11-18-g: no core dumps and no same-uid debugger or ``py-spy``
attach to the api in production.

How it is inherited decides where it is called. ``fork`` keeps the flag, ``exec``
resets it to dumpable. So it must run in every process that holds the api env.
:func:`persona_api.app.create_app` calls it: with ``uvicorn --workers 1`` (the image)
that is the one serving process, PID 1, and spawned uvicorn workers each run the
factory too. The community entrypoint (``python -m persona_api``) calls it as well.
A multi-worker uvicorn master never runs the factory, so it would stay dumpable;
``tests/unit/test_process_hardening.py`` pins the image to one worker for that reason.

Linux only. Anywhere else, or if the call fails, it logs a warning and the api
starts as before: it never blocks startup.

Standard library only: it runs before the app is built.
"""

from __future__ import annotations

import ctypes
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["PR_SET_DUMPABLE", "make_non_dumpable"]

#: ``include/uapi/linux/prctl.h``.
PR_SET_DUMPABLE = 4


def _libc_prctl() -> Callable[..., int]:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    # int prctl(int option, unsigned long arg2, ...): typed, so arg2 is a clean 0.
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl.restype = ctypes.c_int
    typed: Callable[..., int] = prctl
    return typed


def _log_warning(message: str) -> None:
    from persona.logging import get_logger

    get_logger("api.process_hardening").warning(message)


def make_non_dumpable(
    *,
    platform: str | None = None,
    prctl: Callable[..., int] | None = None,
    warn: Callable[[str], None] | None = None,
) -> bool:
    """Mark this process non-dumpable on Linux; return whether it now is.

    Args:
        platform: Defaults to ``sys.platform``; injected in tests.
        prctl: Defaults to libc's ``prctl``; injected in tests.
        warn: Where a failure is reported; defaults to the api logger.

    Returns:
        ``True`` when ``prctl(PR_SET_DUMPABLE, 0)`` succeeded. ``False`` off Linux
        (nothing done) or when the call failed (a warning is logged).
    """
    if not (platform or sys.platform).startswith("linux"):
        return False
    report = warn or _log_warning
    try:
        call = prctl or _libc_prctl()
        result = call(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError) as exc:
        report(f"could not mark the api non-dumpable (prctl unavailable: {exc})")
        return False
    if result != 0:
        report(
            "could not mark the api non-dumpable "
            f"(prctl returned {result}, errno {ctypes.get_errno()}); "
            "built-in MCP children may read its environment"
        )
        return False
    return True
