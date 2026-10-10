"""Test helper: parse a hostile file in a child process with a memory limit.

A test that feeds a hostile file is guarding a bound. If that bound regresses, the parse
can allocate without limit, and in the test process that kills the whole test run (CI
saw the runner die with exit code 137), not one test. Here the parse runs in a child
interpreter with ``RLIMIT_AS`` set, the same limit the production parse worker has, so
a regression fails as one clean test failure that names what happened.

The child returns plain data: the parse result as JSON, a typed domain error (rebuilt
here, so ``pytest.raises`` works as before), or its own death. ``RLIMIT_AS`` is set on
Linux only; elsewhere the child still isolates the test process from a crash.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from persona.documents.errors import (
    CorruptDocumentError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from persona.documents.parsers import ParseResult

if TYPE_CHECKING:
    from pathlib import Path

#: The production parse worker's address-space limit (D-FV0-31).
CHILD_MEMORY_BYTES: int = 1024 * 1024 * 1024

_CHILD = r"""
import importlib, json, pathlib, sys
path, target, memory = sys.argv[1], sys.argv[2], int(sys.argv[3])
if sys.platform.startswith("linux"):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
module_name, _, function_name = target.partition(":")
function = getattr(importlib.import_module(module_name), function_name)

def peak_rss_bytes():
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

try:
    result = function(pathlib.Path(path))
except BaseException as exc:
    out = {
        "kind": "error",
        "type": type(exc).__name__,
        "message": getattr(exc, "message", None) or str(exc)[:300],
        "context": dict(getattr(exc, "context", {}) or {}),
    }
else:
    out = {"kind": "ok", "result": result.model_dump(mode="json")}
out["peak_rss_bytes"] = peak_rss_bytes()
sys.stdout.write("\n" + json.dumps(out) + "\n")
"""

_DOMAIN_ERRORS: dict[str, type[Exception]] = {
    "CorruptDocumentError": CorruptDocumentError,
    "UnsupportedFormatError": UnsupportedFormatError,
    "MissingDependencyError": MissingDependencyError,
}


@dataclass(frozen=True)
class BoundedParse:
    """What the child returned: the result, and the child's peak resident memory."""

    result: ParseResult
    peak_rss_bytes: int


def bounded_parse(
    path: Path,
    *,
    target: str = "persona.documents.parsers:parse_document",
    memory_bytes: int = CHILD_MEMORY_BYTES,
    timeout_seconds: float = 100.0,
) -> BoundedParse:
    """Run ``target(path)`` in a memory-limited child and return its result.

    Raises:
        CorruptDocumentError, UnsupportedFormatError, MissingDependencyError: Rebuilt from
            the child, with their context, as the parser raised them.
        pytest.fail: The child ran out of memory, raised anything else, died or timed
            out. One clean failure, never a dead test run.
    """
    try:
        run = subprocess.run(
            [sys.executable, "-c", _CHILD, str(path), target, str(memory_bytes)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"the bounded parse of {path.name} ran past {timeout_seconds} s")
    lines = [line for line in run.stdout.splitlines() if line.startswith("{")]
    if run.returncode != 0 or not lines:
        pytest.fail(
            f"the bounded parse of {path.name} died (exit code {run.returncode}); "
            f"stderr tail: {run.stderr[-500:]!r}"
        )
    out = json.loads(lines[-1])
    if out["kind"] == "ok":
        return BoundedParse(
            result=ParseResult.model_validate(out["result"]),
            peak_rss_bytes=int(out["peak_rss_bytes"]),
        )
    error = _DOMAIN_ERRORS.get(out["type"])
    if error is None:
        pytest.fail(
            f"the bounded parse of {path.name} raised {out['type']}: {out['message']} "
            f"(peak {out['peak_rss_bytes'] // (1024 * 1024)} MiB of a "
            f"{memory_bytes // (1024 * 1024)} MiB limit)"
        )
    rebuilt = error(out["message"], context=out["context"])
    # The child's peak, for tests that bound memory on the refusal path too.
    rebuilt.peak_rss_bytes = int(out["peak_rss_bytes"])  # type: ignore[attr-defined]
    raise rebuilt
