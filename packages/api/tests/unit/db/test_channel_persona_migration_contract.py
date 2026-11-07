"""Migration 060 takes its locks the way 057 and 058 do: briefly, and never waiting long.

``ALTER TABLE ... ADD CONSTRAINT`` on ``connector_channels`` needs a lock the connector process
holds against while it flips a channel's active persona. Without a ``lock_timeout`` a deploy
behind a long transaction would wait and queue every later query behind it. The constraint is
added ``NOT VALID`` (a brief lock, no scan) and validated afterwards outside that lock. The
Postgres test proves the timeout fires; this pins that both directions carry it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "060_channel_persona_set_null.py"
)


def _body(name: str) -> str:
    tree = ast.parse(_MIGRATION.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.unparse(fn)


def test_upgrade_sets_and_resets_a_lock_timeout_and_validates_separately() -> None:
    body = _body("upgrade")
    assert "SET lock_timeout = '5s'" in body
    assert "RESET lock_timeout" in body
    assert "NOT VALID" in body
    assert "VALIDATE CONSTRAINT" in body
    assert "autocommit_block" in body


def test_downgrade_sets_and_resets_a_lock_timeout_and_validates_separately() -> None:
    body = _body("downgrade")
    assert "SET lock_timeout = '5s'" in body
    assert "RESET lock_timeout" in body
    assert "NOT VALID" in body
    assert "VALIDATE CONSTRAINT" in body
