"""``require_path_segment``: one name that becomes exactly one directory or file name.

An id that ends up joined into a path (an owner id, a persona id, a conversation id) has to
name one entry below its parent and nothing else. ``..`` names the parent, ``.`` names the
directory itself, and a separator names a deeper path, so each of those turns a join into a
different place. The resolver alone does not catch them: ``owner/..`` resolves to the root it
is checked against, which it accepts.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime in fixtures

import pytest
from persona.audit import AuditAction, AuditEvent, JSONLAuditLogger
from persona.errors import SandboxViolationError
from persona.schema.chunks import WriteSource
from persona.tools._sandbox import require_path_segment
from persona.tools.audit import JSONLToolAuditLogger, ToolAuditEvent

_REFUSED = [
    "",
    ".",
    "..",
    "a/b",
    "../x",
    "a\\b",
    "a\x00b",
    "a\nb",
    "C:",
    "name.",
    "name ",
    " name",
    "NUL",
    "con",
    "x" * 201,
]


@pytest.mark.parametrize("value", _REFUSED)
def test_refuses_anything_that_is_not_one_plain_name(value: str) -> None:
    with pytest.raises(SandboxViolationError):
        require_path_segment(value, what="persona id")


@pytest.mark.parametrize(
    "value",
    ["persona_0123456789abcdef0123456789abcdef", "user_2abcDEF", "local-owner", "a.b", "x" * 200],
)
def test_accepts_a_plain_name_and_returns_it_unchanged(value: str) -> None:
    assert require_path_segment(value, what="id") == value


def test_the_refusal_names_what_was_wrong_without_the_whole_value() -> None:
    with pytest.raises(SandboxViolationError) as caught:
        require_path_segment("..", what="persona id")
    assert caught.value.context["what"] == "persona id"
    assert caught.value.context["reason"] == "not_one_name"


def _store_event(persona_id: str) -> AuditEvent:
    from datetime import UTC, datetime

    return AuditEvent(
        timestamp=datetime(2026, 11, 7, tzinfo=UTC),
        persona_id=persona_id,
        action=AuditAction.WRITE,
        store="episodic",
        source=WriteSource.SYSTEM,
    )


def test_the_store_audit_file_is_never_written_outside_its_folder(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = JSONLAuditLogger(root)
    with pytest.raises(SandboxViolationError):
        log.emit(_store_event("../escaped"))
    assert not (tmp_path / "escaped.jsonl").exists()
    with pytest.raises(SandboxViolationError):
        log.read("../escaped")


def test_the_tool_audit_file_is_never_written_outside_its_folder(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    root = tmp_path / "audit"
    log = JSONLToolAuditLogger(root)
    event = ToolAuditEvent(
        timestamp=datetime(2026, 11, 7, tzinfo=UTC),
        persona_id="../escaped",
        tool_name="file_write",
        action="write",
        resource="x",
    )
    with pytest.raises(SandboxViolationError):
        log.emit(event)
    assert not (tmp_path / "escaped.tools.jsonl").exists()
