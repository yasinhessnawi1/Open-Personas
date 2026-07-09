"""Per-component logging built on loguru.

Library code never imports ``loguru.logger`` directly. Callers obtain a logger
through :func:`get_logger`, which returns a logger pre-bound with
``component=<name>``. Sinks are configured **once** at first call, behind an
idempotency flag protected by a lock, so repeated imports — common in test
suites, multi-module apps, and REPL reloads — do not stack duplicate sinks.

The library deliberately does not call ``logger.remove()``. Downstream apps
that also use loguru keep their existing sinks and see ours added alongside.
Per D-01-7 in ``docs/specs/spec_01/decisions.md``.
"""

from __future__ import annotations

import contextlib
import re
import sys
import threading
from typing import TYPE_CHECKING, Final

from loguru import logger as _root_logger

from persona.config import PersonaCoreConfig

if TYPE_CHECKING:
    from loguru import Logger

__all__ = ["get_logger", "redact_secrets", "reset_for_testing"]

# Marker substituted for anything that looks like a credential in a free-form
# string headed for a log. Deliberately ASCII-plain so it round-trips through
# any sink encoding.
_SECRET_MARKER: Final[str] = "<redacted>"

# Default cap for a redacted error message: long enough to keep the operator
# signal (the provider's own phrasing of *what* failed), short enough that a
# multi-KB HTML error body or a stack-embedded token dump cannot flood the log.
_ERROR_MESSAGE_MAX_CHARS: Final[int] = 300

# Patterns that scrub anything resembling a secret out of a provider error
# string BEFORE it reaches a sink. Order matters: the labelled forms
# (``Bearer …`` / ``api_key=…``) run first so their value is masked whole;
# the long-opaque-run catch-all mops up bare keys/hashes/base64 the labelled
# passes missed. Over-redaction here is acceptable — a masked hash still lets
# the operator see the surrounding "invalid model id" / "quota exceeded" text.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # ``Bearer <token>`` (case-insensitive), token = non-space run.
    re.compile(r"(?i)\bbearer\s+[^\s\"']+"),
    # ``sk-…`` / ``sk-or-v1-…`` style keys (OpenAI-compat + OpenRouter).
    re.compile(r"\bsk-[A-Za-z0-9._-]{6,}"),
    # ``api_key=…`` / ``api-key: …`` / ``token=…`` / ``secret=…`` /
    # ``password=…`` / ``authorization: …`` — label + delimiter + value.
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|authorization)"
        r"\s*[=:]\s*[^\s\"',&]+"
    ),
    # Bare long opaque runs (>=40 chars of key/hash/base64 alphabet).
    re.compile(r"\b[A-Za-z0-9._~+/=-]{40,}\b"),
)


def redact_secrets(text: str, *, max_chars: int = _ERROR_MESSAGE_MAX_CHARS) -> str:
    """Scrub credential-looking substrings out of ``text`` and cap its length.

    Defence-in-depth for logging free-form provider error strings (``str(exc)``):
    an upstream SDK can echo the request ``Authorization`` header, a bad
    ``sk-…`` key, or a signed URL into its exception message. This masks the
    known credential shapes (Bearer tokens, ``sk-…`` keys, ``api_key=…`` and
    friends, and long opaque hex/base64 runs) with :data:`_SECRET_MARKER`, then
    truncates to ``max_chars`` (appending an ``…`` ellipsis) so an oversized
    error body cannot flood the sink.

    This is a heuristic, not a guarantee — it errs toward over-redaction. Use it
    at the log boundary, never to sanitise data for storage or return.

    Args:
        text: The raw string (typically ``str(exc)``) about to be logged.
        max_chars: Cap on the returned length; defaults to
            :data:`_ERROR_MESSAGE_MAX_CHARS`.

    Returns:
        The redacted, length-capped string.
    """
    scrubbed = text
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub(_SECRET_MARKER, scrubbed)
    if len(scrubbed) > max_chars:
        scrubbed = scrubbed[:max_chars] + "…"
    return scrubbed


# Idempotency guards. These two variables together implement the
# "configure sinks exactly once per process" property required by D-01-7.
# The lock is held only briefly during the check-and-set; the work inside
# is a few `logger.add` calls, so contention is negligible in practice.
_configured: bool = False
_lock: threading.Lock = threading.Lock()
# Sink ids returned by `logger.add()`; tracked so `reset_for_testing` can
# remove only what we added without touching downstream sinks.
_sink_ids: list[int] = []

# Pretty format mirrors the loguru default but adds the bound ``component``.
_PRETTY_FORMAT: str = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[component]}</cyan> | "
    "<level>{message}</level>"
)


def _configure_sinks(config: PersonaCoreConfig) -> None:
    """Add the configured sinks to the root loguru logger.

    Caller MUST hold ``_lock``. Does not check the idempotency flag — that is
    the caller's responsibility (see :func:`get_logger`).
    """
    if config.log_format == "json":
        sink_id = _root_logger.add(
            sys.stderr,
            level=config.log_level,
            serialize=True,
            backtrace=False,
            diagnose=False,
        )
    else:
        sink_id = _root_logger.add(
            sys.stderr,
            level=config.log_level,
            format=_PRETTY_FORMAT,
            backtrace=False,
            diagnose=False,
        )
    _sink_ids.append(sink_id)

    if config.log_file is not None:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        if config.log_format == "json":
            file_sink_id = _root_logger.add(
                config.log_file,
                level=config.log_level,
                serialize=True,
                backtrace=False,
                diagnose=False,
                enqueue=True,
            )
        else:
            file_sink_id = _root_logger.add(
                config.log_file,
                level=config.log_level,
                format=_PRETTY_FORMAT,
                backtrace=False,
                diagnose=False,
                enqueue=True,
            )
        _sink_ids.append(file_sink_id)


def get_logger(component: str, *, config: PersonaCoreConfig | None = None) -> Logger:
    """Return a loguru logger bound to ``component``.

    The first call (process-wide) configures sinks from ``config`` (or a
    freshly-constructed :class:`PersonaCoreConfig` if not provided). Subsequent
    calls reuse the existing configuration. The check-and-set is locked so
    concurrent calls cannot race past the idempotency guard and stack
    duplicate sinks.

    Args:
        component: Dotted lowercase identifier mirroring the package path.
            Examples: ``"stores.episodic"``, ``"audit"``, ``"cli"``.
        config: Optional config override, mainly for tests. Production code
            should let this default and configure via env vars.

    Returns:
        A loguru ``Logger`` with ``component`` already bound.
    """
    global _configured  # noqa: PLW0603 — idempotency flag is the whole point

    if not _configured:
        with _lock:
            if not _configured:
                _configure_sinks(config or PersonaCoreConfig())
                _configured = True

    # `bind` returns a child logger with the extra fields attached.
    return _root_logger.bind(component=component)


def reset_for_testing() -> None:
    """Remove sinks added by this module and clear the idempotency flag.

    Test-only helper. Production code must not call this — it would defeat
    the singleton guarantee. Removes only sinks this module added; never
    touches sinks owned by downstream code.
    """
    global _configured  # noqa: PLW0603 — symmetric reset for the flag above
    with _lock:
        for sink_id in _sink_ids:
            with contextlib.suppress(ValueError):
                # ValueError = sink already removed by an earlier reset.
                _root_logger.remove(sink_id)
        _sink_ids.clear()
        _configured = False
