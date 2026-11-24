"""Migration 070 adds what the model says: two nullable text columns on inflight_ops (R9-318)."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType

from persona_api.db.models import inflight_ops

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "070_inflight_op_boot.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("inflight_op_boot_070", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_070_follows_069() -> None:
    module = _migration()
    assert module.revision == "070_inflight_op_boot"
    assert module.down_revision == "069_persona_create_complete"


def test_the_model_columns_are_nullable_text() -> None:
    """Nullable: a slot written by pre-070 code, or by the old process during the deploy, has
    neither, and the boot sweep treats it as an earlier boot's."""
    for name in ("instance_id", "boot_id"):
        column = inflight_ops.c[name]
        assert column.nullable is True
        assert column.server_default is None
    text = _MIGRATION.read_text(encoding="utf-8")
    for name in ("instance_id", "boot_id"):
        assert f'"{name}"' in text


def test_070_fails_fast_on_a_busy_inflight_ops_table() -> None:
    """As 057, 058 and 069: a 5 s lock_timeout, reset afterwards, in both directions."""
    module = _migration()
    for step in (module.upgrade, module.downgrade):
        body = inspect.getsource(step)
        assert "SET lock_timeout = '5s'" in body
        assert body.rstrip().endswith('op.execute("RESET lock_timeout")')
