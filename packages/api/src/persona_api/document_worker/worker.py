"""The document parse worker process (D-FV0-31): ``python -m persona_api.document_worker.worker``.

The api starts this as a fresh interpreter with an allowlisted environment, so it holds
no database URL, no provider key and no other secret, and it never opens a database or
the storage backend. It reads jobs on stdin and answers on stdout, one at a time, until
stdin closes.

Before it reads any job it limits itself, where the platform allows:

- Linux: address space (``RLIMIT_AS``), CPU seconds per job (``RLIMIT_CPU``), and no
  core dumps (``RLIMIT_CORE``).
- macOS: CPU seconds per job and no core dumps. macOS does not enforce ``RLIMIT_AS``,
  so the memory limit is not applied there.
- Windows: none; there is no ``resource`` module. Crash isolation and the api's
  wall-clock kill still apply.

The ready message reports which limits took effect, and the api logs it.

A file that exhausts a limit kills this process, not the api: ``RLIMIT_CPU`` ends it with
``SIGXCPU``, and an allocation past ``RLIMIT_AS`` fails with ``MemoryError`` (answered as
a limit, after which the worker exits) or kills a native library outright.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import IO, Any

from persona_api.document_worker.protocol import FrameError, read_frame, write_frame

__all__ = ["main"]


def _private_stdout() -> IO[bytes]:
    """Keep the real stdout for frames, and point fd 1 at stderr.

    Anything a library prints, from Python or from native code, then lands on stderr
    and can never corrupt a frame.
    """
    frames_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(frames_fd, "wb")


def _apply_limits(memory_bytes: int, cpu_seconds: int) -> dict[str, bool]:
    """Set this process's limits; report which ones took effect."""
    applied = {"memory": False, "cpu": False, "core": False}
    try:
        import resource  # noqa: PLC0415 (POSIX only)
    except ImportError:
        return applied
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        applied["core"] = True
    except (ValueError, OSError):
        pass
    if sys.platform.startswith("linux") and memory_bytes > 0:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            applied["memory"] = True
        except (ValueError, OSError):
            pass
    if cpu_seconds > 0:
        # Armed before each job (see main), never once here: RLIMIT_CPU counts the whole
        # life of the process, so a single allowance would run out across jobs.
        try:
            resource.getrlimit(resource.RLIMIT_CPU)
            applied["cpu"] = True
        except (ValueError, OSError):
            pass
    return applied


def _arm_cpu_limit(cpu_seconds: int) -> None:
    """Allow ``cpu_seconds`` more CPU from now; past it the kernel sends ``SIGXCPU``.

    ``RLIMIT_CPU`` counts the process's whole life, so the soft limit is moved to
    "used so far plus the allowance" before each job. The hard limit is left alone, so
    the soft one can be moved again for the next job.
    """
    import resource  # noqa: PLC0415 (POSIX only)

    usage = resource.getrusage(resource.RUSAGE_SELF)
    used = int(usage.ru_utime + usage.ru_stime) + 1
    _, hard = resource.getrlimit(resource.RLIMIT_CPU)
    soft = used + cpu_seconds
    if hard != resource.RLIM_INFINITY:
        soft = min(soft, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))


def _volunteer_for_the_oom_killer() -> bool:
    """Linux: make this process the kernel's first choice when memory runs out.

    If the machine runs short despite the limits, the worker is killed, not the api.
    Raising one's own ``oom_score_adj`` needs no privilege.
    """
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:  # noqa: PTH123
            handle.write("1000")
    except OSError:
        return False
    return True


def _quiet() -> None:
    """Silence library logging and warnings: a hostile file can produce millions."""
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")


def _error(kind: str, exc: BaseException) -> dict[str, Any]:
    context = getattr(exc, "context", None)
    return {
        "kind": "error",
        "error": kind,
        "message": getattr(exc, "message", None) or str(exc)[:500],
        "context": {str(k): str(v) for k, v in context.items()}
        if isinstance(context, dict)
        else {},
        "exception": type(exc).__name__,
    }


def _run_job(job: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Parse one job. Returns the reply and whether this process should exit after it."""
    from persona.documents.errors import (  # noqa: PLC0415
        CorruptDocumentError,
        MissingDependencyError,
        UnsupportedFormatError,
    )

    from persona_api.document_worker.untrusted import parse_untrusted_document  # noqa: PLC0415

    render_dir = job.get("render_dir")
    try:
        parsed = parse_untrusted_document(
            Path(job["path"]),
            render_dir=Path(render_dir) if render_dir else None,
            dpi=int(job["dpi"]),
        )
    except UnsupportedFormatError as exc:
        return _error("unsupported", exc), False
    except CorruptDocumentError as exc:
        return _error("corrupt", exc), False
    except MissingDependencyError as exc:
        return _error("missing_dependency", exc), False
    except MemoryError as exc:
        # The address-space limit held. The heap may be in any state now, so answer and
        # leave; the api starts a fresh worker for the next job.
        return _error("memory_limit", exc), True
    except Exception as exc:  # noqa: BLE001 (answered as a typed failure, never raised)
        return _error("internal", exc), True
    return (
        {
            "kind": "ok",
            "parse_result": parsed.parse_result.model_dump(mode="json"),
            "page_images": [str(p) for p in parsed.page_images],
        },
        False,
    )


def main(argv: list[str] | None = None) -> int:
    """Serve parse jobs until stdin closes."""
    parser = argparse.ArgumentParser(prog="persona-document-worker")
    parser.add_argument("--memory-bytes", type=int, default=0)
    parser.add_argument("--cpu-seconds", type=int, default=0)
    args = parser.parse_args(argv)

    frames = _private_stdout()
    jobs = sys.stdin.buffer
    applied = _apply_limits(args.memory_bytes, args.cpu_seconds)
    applied["oom_first"] = sys.platform.startswith("linux") and _volunteer_for_the_oom_killer()
    _quiet()
    # Import the parsers now, under the limits, so the first job does not pay for it.
    import persona_api.document_worker.untrusted  # noqa: F401, PLC0415

    write_frame(frames, {"kind": "ready", "pid": os.getpid(), "limits": applied})
    while True:
        job = read_frame(jobs)
        if job is None:
            return 0
        if applied["cpu"]:
            _arm_cpu_limit(args.cpu_seconds)
        reply, leave = _run_job(job)
        reply["leaving"] = leave
        reply["job_id"] = job.get("job_id")
        try:
            write_frame(frames, reply)
        except FrameError:
            # Over the frame cap: never send it. The api refuses the document instead.
            write_frame(
                frames,
                {
                    "kind": "error",
                    "error": "reply_too_large",
                    "message": "the parse result is larger than the api accepts",
                    "context": {},
                    "exception": "FrameError",
                    "leaving": True,
                    "job_id": job.get("job_id"),
                },
            )
            return 0
        if leave:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
