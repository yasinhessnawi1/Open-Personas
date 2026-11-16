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
