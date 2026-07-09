"""Tests for ``persona.logging`` — D-01-7 enforcement.

The D-01-7 guardrail: ``get_logger()`` must configure sinks exactly once per
process. The check-and-set is locked so concurrent imports cannot race past
the idempotency guard. Test names describe the expected behaviour rather
than the implementation.

Capture strategy: we capture stderr by using pytest's ``capsys`` fixture
combined with reading the loguru output after a ``reset_for_testing()`` flush.
``capsys`` redirects ``sys.stderr`` at the file-descriptor level before the
test body runs, so loguru sinks added inside the test write into the capture
buffer. ``reset_for_testing()`` is called inside each test before reading to
ensure ``enqueue=True`` sinks flush.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import pytest
from persona import logging as plog
from persona.config import PersonaCoreConfig
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_logging_state() -> Generator[None, None, None]:
    """Reset module-level logging state between tests.

    Without this, the order of test execution would change behaviour
    because ``_configured`` is process-wide.
    """
    plog.reset_for_testing()
    yield
    plog.reset_for_testing()


def _capture_stderr(capsys: pytest.CaptureFixture[str]) -> str:
    """Flush any enqueued sinks and read captured stderr."""
    plog.reset_for_testing()
    return capsys.readouterr().err


def test_get_logger_binds_component_name(capsys: pytest.CaptureFixture[str]) -> None:
    log = plog.get_logger("stores.episodic", config=PersonaCoreConfig(log_format="pretty"))
    log.info("hello")
    output = _capture_stderr(capsys)
    assert "stores.episodic" in output
    assert "hello" in output


def test_get_logger_repeated_calls_do_not_stack_sinks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The D-01-7 guardrail: many imports, one set of sinks."""
    for _ in range(10):
        plog.get_logger("schema", config=PersonaCoreConfig(log_format="pretty"))

    log = plog.get_logger("schema", config=PersonaCoreConfig(log_format="pretty"))
    log.info("one-line")

    output = _capture_stderr(capsys)
    lines = [line for line in output.splitlines() if "one-line" in line]
    assert len(lines) == 1, f"expected exactly one log line, got {len(lines)}: {lines!r}"


def test_get_logger_is_threadsafe_under_concurrent_first_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """50 threads call get_logger concurrently; one emit produces one stderr line.

    Exercises the lock around the check-and-set. Without the lock, multiple
    threads could observe ``_configured == False`` and each call
    ``_configure_sinks``, stacking duplicate sinks.
    """
    config = PersonaCoreConfig(log_format="pretty")
    barrier = threading.Barrier(50)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            plog.get_logger("concurrent.test", config=config)
        except BaseException as exc:  # noqa: BLE001 — capture for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"worker errors: {errors!r}"

    log = plog.get_logger("concurrent.test", config=config)
    log.info("single")

    output = _capture_stderr(capsys)
    lines = [line for line in output.splitlines() if "single" in line]
    assert len(lines) == 1, (
        f"concurrent first-calls stacked sinks: 'single' appeared "
        f"{len(lines)} times instead of once"
    )


def test_json_format_emits_valid_jsonl_with_component(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = plog.get_logger("audit", config=PersonaCoreConfig(log_format="json"))
    log.info("payload")

    raw = _capture_stderr(capsys).strip()
    assert raw, "expected at least one JSON line"

    # The capture buffer holds exactly one record for this test.
    record = json.loads(raw)
    # loguru's serialize=True nests under "record".
    assert record["record"]["extra"]["component"] == "audit"
    assert record["record"]["message"] == "payload"


def test_pretty_format_contains_component_name(capsys: pytest.CaptureFixture[str]) -> None:
    log = plog.get_logger("cli", config=PersonaCoreConfig(log_format="pretty"))
    log.info("ready")
    output = _capture_stderr(capsys)
    assert "cli" in output
    assert "ready" in output
    assert "INFO" in output


def test_log_file_sink_writes_alongside_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "persona.log"
    config = PersonaCoreConfig(log_format="pretty", log_file=log_file)

    log = plog.get_logger("registry", config=config)
    log.info("file+stderr")

    # `enqueue=True` on the file sink means writes are async; flushing via
    # reset removes the sink and forces a flush. Read stderr after the flush.
    output = _capture_stderr(capsys)

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "file+stderr" in content
    assert "registry" in content
    assert "file+stderr" in output


def test_invalid_log_format_rejected_at_config_construction() -> None:
    """``PERSONA_LOG_FORMAT`` outside the literal raises at config time."""
    with pytest.raises(ValidationError, match="log_format"):
        PersonaCoreConfig(log_format="xml")  # type: ignore[arg-type]


def test_get_logger_uses_default_config_when_none_provided(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No explicit config + no env overrides → pretty format, INFO level."""
    log = plog.get_logger("schema")
    log.info("defaulted")
    output = _capture_stderr(capsys)
    assert "defaulted" in output
    assert "schema" in output


def test_no_exc_info_true_in_log_calls_repo_wide() -> None:
    """Guard (R9-016): ``exc_info=True`` is a stdlib-logging kwarg that our loguru
    loggers SILENTLY IGNORE — no traceback is emitted. Eight fail-soft warnings had it
    and dropped their tracebacks for months. The correct idiom is
    ``logger.opt(exception=True).warning(...)``. This scan fails if the anti-pattern
    reappears anywhere in shipped source.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]
    offenders: list[str] = []
    for src in repo_root.glob("packages/*/src/**/*.py"):
        for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            if "exc_info=True" in line:
                offenders.append(f"{src.relative_to(repo_root)}:{i}")
    assert not offenders, (
        "exc_info=True is a no-op on loguru loggers (drops the traceback). "
        "Use `logger.opt(exception=True).warning(...)` instead. Offenders:\n"
        + "\n".join(offenders)
    )
