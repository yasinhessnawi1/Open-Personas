"""The owner's own store audit after an account erasure (R9-284 T9, review M1).

The rows need Postgres (the table's id default) and are checked in CI
(tests/integration/test_account_erasure_postgres.py); the file move is checked here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona_api.services.account_erasure_audit import (
    rekey_store_audit_file,
)

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "user_alice"
_ERASURE = "erasure_0123"
_NOW = "2026-11-16T12:00:00+00:00"


def _event(persona_id: str, **extra: object) -> dict[str, object]:
    return {
        "timestamp": _NOW,
        "persona_id": persona_id,
        "action": "write",
        "store": "graph",
        "source": "system",
        "written_by": "persona_x",
        "reason": "the model's words",
        "metadata": {"verbatim_ask": "what the user typed", "label": "kept"},
        **extra,
    }


def test_the_owners_file_moves_to_the_erasure_id_rekeyed(tmp_path: Path) -> None:
    source = tmp_path / f"{_OWNER}.jsonl"
    source.write_text(
        "\n".join(json.dumps(e) for e in (_event(_OWNER), _event(_OWNER))) + "\n",
        encoding="utf-8",
    )

    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 2

    assert not source.exists(), "no file is named after the account any more"
    moved = (tmp_path / f"{_ERASURE}.jsonl").read_text(encoding="utf-8")
    assert _OWNER not in moved
    assert "the model's words" not in moved
    assert "what the user typed" not in moved
    events = [json.loads(line) for line in moved.splitlines()]
    assert {e["persona_id"] for e in events} == {_ERASURE}
    assert all(e["metadata"] == {"label": "kept"} for e in events)


def test_a_resumed_move_finds_nothing_to_move(tmp_path: Path) -> None:
    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 0
    assert rekey_store_audit_file(None, owner_id=_OWNER, erasure_id=_ERASURE) == 0


def test_the_owner_as_author_in_a_persona_log_is_rekeyed(tmp_path: Path) -> None:
    """Review M-1: the owner's id as ``written_by`` in the persona's own log."""
    from persona_api.services.account_erasure_audit import (  # noqa: PLC0415
        rekey_persona_audit_files,
    )

    log = tmp_path / "persona_x.jsonl"
    events = [
        _event("persona_x", written_by=_OWNER, reason="initial persona load"),
        _event("persona_x", written_by="persona_x"),
    ]
    log.write_bytes(("\n".join(json.dumps(e) for e in events) + "\n").encode())

    changed = rekey_persona_audit_files(
        tmp_path, ["persona_x", "persona_gone"], owner_id=_OWNER, erasure_id=_ERASURE
    )

    assert changed == 2, "every line is rewritten (content keys scrubbed), two in all"
    text = log.read_text(encoding="utf-8")
    assert _OWNER not in text
    first, second = (json.loads(line) for line in text.splitlines())
    assert first["written_by"] == _ERASURE
    assert first["persona_id"] == "persona_x", "the persona's own id is kept"
    assert second["written_by"] == "persona_x"
    assert not list(tmp_path.glob("*.rekeying")), "no temporary file left"


def test_the_moved_owner_file_rekeys_the_author_too(tmp_path: Path) -> None:
    source = tmp_path / f"{_OWNER}.jsonl"
    source.write_bytes((json.dumps(_event(_OWNER, written_by=_OWNER)) + "\n").encode())

    rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE)

    moved = (tmp_path / f"{_ERASURE}.jsonl").read_text(encoding="utf-8")
    assert _OWNER not in moved


# ---- crash safety (review L-1) ---------------------------------------------------------


def _owner_log(tmp_path: Path, n: int = 2) -> Path:
    source = tmp_path / f"{_OWNER}.jsonl"
    events = [_event(_OWNER, reason=f"event {i}") for i in range(n)]
    source.write_bytes(("\n".join(json.dumps(e) for e in events) + "\n").encode())
    return source


def test_a_crash_after_the_rename_is_finished_on_resume(tmp_path: Path) -> None:
    """The run died after renaming to .moving: the resume lands it, nothing is lost."""
    source = _owner_log(tmp_path)
    moving = tmp_path / f"{_ERASURE}.jsonl.moving"
    source.replace(moving)  # where the crash left it

    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 2

    target = tmp_path / f"{_ERASURE}.jsonl"
    assert not moving.exists()
    assert len(target.read_text(encoding="utf-8").splitlines()) == 2
    assert _OWNER not in target.read_text(encoding="utf-8")


def test_a_crash_after_the_append_appends_nothing_twice(tmp_path: Path) -> None:
    """The run died after appending but before removing .moving and its offset: the resume
    cuts the target back to the offset and lands the lines once."""
    _owner_log(tmp_path)
    rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE)
    target = tmp_path / f"{_ERASURE}.jsonl"
    landed = target.read_bytes()
    # Rebuild the crash: the same events still in .moving, the offset (0) still recorded.
    source = _owner_log(tmp_path)
    source.replace(tmp_path / f"{_ERASURE}.jsonl.moving")
    (tmp_path / f"{_ERASURE}.jsonl.moving.offset").write_text("0", encoding="ascii")

    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 2

    assert target.read_bytes() == landed, "nothing appended twice"
    assert not (tmp_path / f"{_ERASURE}.jsonl.moving").exists()
    assert not (tmp_path / f"{_ERASURE}.jsonl.moving.offset").exists()


def test_a_crash_mid_append_resumes_to_a_clean_file(tmp_path: Path) -> None:
    """A torn tail after the recorded offset is cut away before the lines land again."""
    target = tmp_path / f"{_ERASURE}.jsonl"
    kept = (json.dumps({"persona_id": _ERASURE, "action": "earlier"}) + "\n").encode()
    target.write_bytes(kept + b'{"persona_id": "erasure_0123", "act')  # the torn tail
    (tmp_path / f"{_ERASURE}.jsonl.moving.offset").write_text(str(len(kept)), encoding="ascii")
    source = _owner_log(tmp_path)
    source.replace(tmp_path / f"{_ERASURE}.jsonl.moving")

    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 2

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == kept.decode().rstrip("\n"), "what was there before is kept"
    assert len(lines) == 3
    assert all(json.loads(line) for line in lines), "no torn line survives"


def test_two_events_identical_after_rekeying_are_both_kept(tmp_path: Path) -> None:
    """Round 3 LOW-1: re-keying clears free text, so two genuine events can become equal
    lines; the second, moved later, lands next to its twin (a line dedupe dropped it)."""
    source = tmp_path / f"{_OWNER}.jsonl"
    source.write_bytes((json.dumps(_event(_OWNER, reason="first")) + "\n").encode())
    rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE)
    source.write_bytes((json.dumps(_event(_OWNER, reason="second")) + "\n").encode())

    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 1

    lines = (tmp_path / f"{_ERASURE}.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == lines[1], "equal after re-keying, and both kept"


def test_events_written_after_a_move_are_moved_by_the_next_run(tmp_path: Path) -> None:
    _owner_log(tmp_path, n=1)
    rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE)
    late = tmp_path / f"{_OWNER}.jsonl"
    later = {**_event(_OWNER), "timestamp": "2026-11-16T12:05:00+00:00"}  # a later event
    late.write_bytes((json.dumps(later) + "\n").encode())

    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 1

    target = (tmp_path / f"{_ERASURE}.jsonl").read_text(encoding="utf-8")
    assert len(target.splitlines()) == 2
    assert not late.exists()
    assert not list(tmp_path.glob("*.tmp")), "no temporary file left"


def test_the_persona_log_is_rewritten_under_the_loggers_lock(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Round 3 LOW-3 (the reviewer's M7): no append from this process can land between the
    read and the replace, because the rewrite happens while the loggers' lock is held."""
    from contextlib import contextmanager  # noqa: PLC0415

    from persona_api.services import account_erasure_audit as audit  # noqa: PLC0415

    held: list[bool] = [False]
    seen: list[bool] = []

    @contextmanager
    def lock(_path: Path) -> object:
        held[0] = True
        try:
            yield
        finally:
            held[0] = False

    real_write = audit._fsync_write  # noqa: SLF001

    def write(path: Path, payload: bytes, *, append: bool) -> None:
        seen.append(held[0])
        real_write(path, payload, append=append)

    monkeypatch.setattr(audit, "jsonl_file_lock", lock)  # type: ignore[attr-defined]
    monkeypatch.setattr(audit, "_fsync_write", write)  # type: ignore[attr-defined]
    log = tmp_path / "persona_x.jsonl"
    log.write_bytes((json.dumps(_event("persona_x", written_by=_OWNER)) + "\n").encode())

    audit.rekey_persona_audit_files(tmp_path, ["persona_x"], owner_id=_OWNER, erasure_id=_ERASURE)

    assert seen == [True], "the rewrite (fsynced) happened inside the lock"
    assert _OWNER not in log.read_text(encoding="utf-8")


def test_a_first_move_that_died_before_its_append_resumes(tmp_path: Path) -> None:
    """Round 4 MEDIUM-1: an offset of 0 recorded, .moving in place, no target created yet.
    The resume used to fail for ever opening the missing target; it lands the lines."""
    (tmp_path / f"{_ERASURE}.jsonl.moving.offset").write_text("0", encoding="ascii")
    _owner_log(tmp_path, n=3).replace(tmp_path / f"{_ERASURE}.jsonl.moving")

    assert rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE) == 3

    target = tmp_path / f"{_ERASURE}.jsonl"
    assert len(target.read_text(encoding="utf-8").splitlines()) == 3
    assert not (tmp_path / f"{_ERASURE}.jsonl.moving").exists()
    assert not (tmp_path / f"{_ERASURE}.jsonl.moving.offset").exists()


def test_a_lost_target_with_a_later_offset_is_refused(tmp_path: Path) -> None:
    """Anything but 0 for a target that is not there means the log was lost: say so."""
    import pytest  # noqa: PLC0415

    (tmp_path / f"{_ERASURE}.jsonl.moving.offset").write_text("120", encoding="ascii")
    _owner_log(tmp_path).replace(tmp_path / f"{_ERASURE}.jsonl.moving")

    with pytest.raises(OSError, match="gone"):
        rekey_store_audit_file(tmp_path, owner_id=_OWNER, erasure_id=_ERASURE)
