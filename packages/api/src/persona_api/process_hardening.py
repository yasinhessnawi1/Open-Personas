"""Process-level hardening for the api (second security review F5).

The document parse worker runs as the same OS user as the api. On Linux that user can,
by default, attach a debugger to the api or read ``/proc/<api pid>/environ`` and
``/proc/<api pid>/mem``: a worker compromised through a native parsing bug could read
every secret the api holds. Marking the api non-dumpable (``PR_SET_DUMPABLE 0``) makes
the kernel refuse all of that to processes of the same user, and stops core dumps.

What it costs: ``py-spy``, ``gdb -p`` and other ptrace-based tools can no longer attach
to the api without root, and the api writes no core dump when it crashes. A child the
api starts with ``exec`` (the worker) is dumpable again, which is fine: it holds no
secret.
"""

from __future__ import annotations

import sys

from persona.logging import get_logger

__all__ = ["make_process_non_dumpable"]

_log = get_logger("api.hardening")

_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4


def make_process_non_dumpable() -> bool:
    """Mark this process non-dumpable on Linux; whether it now is.

    Elsewhere (macOS, Windows) there is no such switch and this returns ``False``.
    """
    if not sys.platform.startswith("linux"):
        return False
    import ctypes  # noqa: PLC0415

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            _log.warning("could not mark the api non-dumpable: errno {}", ctypes.get_errno())
            return False
        return bool(libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) == 0)
    except (OSError, AttributeError):
        _log.warning("could not mark the api non-dumpable: prctl is unavailable")
        return False
