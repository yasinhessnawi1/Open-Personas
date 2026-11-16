"""The JSONL store audit is rewritten line by line, and only by newline (R9-284 review M-1).

``str.splitlines`` also splits on U+2028, U+2029, U+0085 and others, which pydantic's JSON
leaves raw inside a string. Splitting on them cut one event into fragments that did not parse,
so its free text survived and the record was corrupted, while the step reported success.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path  # noqa: TC003 - a fixture annotation

import pytest
from persona.audit import jsonl_file_lock
from persona_api.services.erasure_audit import scrub_store_audit_files

_OWNER = "user_alice"
_PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.mark.parametrize("separator", [" ", " ", "\u0085", "\x1c", "\x0b"])
def test_a_reason_with_a_unicode_line_separator_is_scrubbed_whole(
    tmp_path: Path, separator: str
) -> None:
    reason = f"the words{separator}the user said"
    events = [
        {"persona_id": _PERSONA, "chunk_ids": ["c1"], "reason": reason},
        {"persona_id": _PERSONA, "chunk_ids": ["c2"], "reason": None},
    ]
    log = tmp_path / f"{_PERSONA}.jsonl"
    # As pydantic writes them: the separator stays raw inside the JSON string.
    log.write_bytes(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
    )

    changed = scrub_store_audit_files(tmp_path, owner_id=_OWNER, persona_id=_PERSONA)

    lines = log.read_bytes().decode("utf-8").split("\n")
    assert lines[-1] == ""
    parsed = [json.loads(line) for line in lines[:-1]]
    assert changed == 1
    assert [e["chunk_ids"] for e in parsed] == [["c1"], ["c2"]]
    assert all(e["reason"] is None for e in parsed)


def test_bytes_that_are_not_utf8_are_carried_through_unchanged(tmp_path: Path) -> None:
    """Review M-2: one bad byte must not make the step fail on every attempt."""
    log = tmp_path / f"{_PERSONA}.jsonl"
    bad = b'{"persona_id":"other","note":"\xff\xfe"}\n'
    ours = json.dumps({"persona_id": _PERSONA, "reason": "said it"}).encode() + b"\n"
    log.write_bytes(bad + ours)

    changed = scrub_store_audit_files(tmp_path, owner_id=_OWNER, persona_id=_PERSONA)

    assert changed == 1
    first, second, end = log.read_bytes().split(b"\n")
    assert first + b"\n" == bad
    assert json.loads(second)["reason"] is None
    assert end == b""


def test_the_rewrite_holds_the_loggers_lock(tmp_path: Path) -> None:
    """Review M-2: an append waits for the rewrite, so it is never lost by the replace."""
    log = tmp_path / f"{_PERSONA}.jsonl"
    log.write_bytes(json.dumps({"persona_id": _PERSONA, "reason": "said it"}).encode() + b"\n")
    finished = threading.Event()

    def _scrub() -> None:
        scrub_store_audit_files(tmp_path, owner_id=_OWNER, persona_id=_PERSONA)
        finished.set()

    with jsonl_file_lock(log):
        worker = threading.Thread(target=_scrub)
        worker.start()
        assert not finished.wait(timeout=0.5), "the scrub ran while a writer held the file"
    worker.join(timeout=30)

    assert finished.is_set()
    assert json.loads(log.read_text(encoding="utf-8"))["reason"] is None


def test_the_logger_appends_under_the_same_lock(tmp_path: Path) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    from persona.audit import AuditAction, AuditEvent, JSONLAuditLogger  # noqa: PLC0415
    from persona.schema.chunks import WriteSource  # noqa: PLC0415

    logger = JSONLAuditLogger(tmp_path)
    event = AuditEvent(
        timestamp=datetime.now(UTC),
        persona_id=_PERSONA,
        action=AuditAction.WRITE,
        store="episodic",
        source=WriteSource.USER,
        chunk_ids=["c"],
    )
    appended = threading.Event()

    def _append() -> None:
        logger.emit(event)
        appended.set()

    with jsonl_file_lock(tmp_path / f"{_PERSONA}.jsonl"):
        writer = threading.Thread(target=_append)
        writer.start()
        assert not appended.wait(timeout=0.5), "the logger appended past the lock"
    writer.join(timeout=30)
    assert appended.is_set()


def test_an_event_without_metadata_is_cleaned_and_stays_a_valid_event(tmp_path: Path) -> None:
    """Review L3: the in-place clean never adds ``"metadata": null`` (AuditEvent wants a dict)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from persona.audit import AuditEvent  # noqa: PLC0415
    from persona_api.services.erasure_audit import scrub_persona_log_in_place  # noqa: PLC0415

    event = {
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "persona_id": _PERSONA,
        "action": "write",
        "store": "self_facts",
        "source": "user",
        "reason": "said it",
    }
    log = tmp_path / f"{_PERSONA}.jsonl"
    log.write_text(json.dumps(event) + "\n", encoding="utf-8")

    scrub_persona_log_in_place(tmp_path, persona_id=_PERSONA, content_keys=frozenset({"x"}))

    cleaned = json.loads(log.read_text(encoding="utf-8"))
    assert "metadata" not in cleaned
    assert cleaned["reason"] is None
    AuditEvent.model_validate(cleaned)
