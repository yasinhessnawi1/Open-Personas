"""Migration 069 adds what the model says: personas.create_complete, true by default (R9-293)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from persona_api.db.models import personas

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "069_persona_create_complete.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("persona_create_complete_069", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_069_follows_068() -> None:
    module = _migration()
    assert module.revision == "069_persona_create_complete"
    assert module.down_revision == "068_forgotten_conversations"


def test_the_model_column_is_complete_unless_a_create_says_otherwise() -> None:
    column = personas.c.create_complete
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "true", "existing rows stay complete"
    text = _MIGRATION.read_text(encoding="utf-8")
    assert 'server_default=sa.text("true")' in text
    assert "nullable=False" in text


def test_069_fails_fast_on_a_busy_personas_table() -> None:
    """Round 2 LOW-2, as 057 and 058: a 5 s lock_timeout, reset afterwards, both directions."""
    import inspect  # noqa: PLC0415

    module = _migration()
    for step in (module.upgrade, module.downgrade):
        body = inspect.getsource(step)
        assert body.index("SET lock_timeout = '5s'") < body.index("_has_column()")
        assert body.rstrip().endswith('op.execute("RESET lock_timeout")')


def test_069_downgrade_deletes_unfinished_creates_before_the_column_goes() -> None:
    """Round 3, Finding 8: older code would list a half-made persona, so it goes first."""
    import inspect  # noqa: PLC0415

    body = inspect.getsource(_migration().downgrade)
    assert body.index("DELETE FROM {_TABLE} WHERE NOT {_COLUMN}") < body.index("drop_column")
