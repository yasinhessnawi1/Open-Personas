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
