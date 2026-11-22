"""Migration 069 adds what the model says: personas.create_complete, true by default (R9-293)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

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


def test_069_downgrade_opens_the_erasures_before_it_deletes() -> None:
    """R9-322: what no cascade reaches is erased by an erasure, so it is opened first."""
    import inspect  # noqa: PLC0415

    body = inspect.getsource(_migration().downgrade)
    assert body.index("open_erasures(") < body.index("DELETE FROM {_TABLE} WHERE NOT {_COLUMN}")


_OWNER = "user_069"
_HALF = "persona_" + "1" * 32
_DONE = "persona_" + "2" * 32
_ALREADY = "persona_" + "3" * 32
_NOT_MINTED = "p_half"


def test_069_downgrade_opens_one_erasure_per_unfinished_create(
    tmp_path: Path,
    erase_templates: Any,  # noqa: ANN401 - the SeedTemplates fixture
) -> None:
    """Each unfinished, minted persona gets an open erasure holding its ids; a complete one,
    one with an erasure already open and an id the erasure would refuse get none."""
    from persona_api.db.models import conversations, users  # noqa: PLC0415
    from persona_api.services.erasure_ledger import (  # noqa: PLC0415
        ErasureLedger,
        ErasureState,
        SubjectKind,
    )
    from sqlalchemy import insert  # noqa: PLC0415

    engine = erase_templates.community_engine(tmp_path)
    ledger = ErasureLedger(engine)
    try:
        with engine.begin() as conn:
            conn.execute(insert(users).values(id=_OWNER, email="u069@x.test"))
            for pid, complete in (
                (_HALF, False),
                (_DONE, True),
                (_ALREADY, False),
                (_NOT_MINTED, False),
            ):
                conn.execute(
                    insert(personas).values(
                        id=pid, owner_id=_OWNER, yaml="y", create_complete=complete
                    )
                )
            conn.execute(
                insert(conversations).values(id="conv_half", owner_id=_OWNER, persona_id=_HALF)
            )
        already = ledger.claim(
            owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=_ALREADY, captured={}
        )

        with engine.begin() as conn:
            opened = _migration().open_erasures(conn)

        assert opened == 1
        half = ledger.get(SubjectKind.PERSONA, _HALF)
        assert half is not None
        assert half.owner_id == _OWNER
        assert half.state is ErasureState.PENDING, "the sweep resumes it"
        assert half.captured == {
            "conversations": ["conv_half"],
            "tasks": [],
            "runs": [],
            "event_triggers": [],
        }
        assert ledger.get(SubjectKind.PERSONA, _DONE) is None, "a complete persona stays"
        assert ledger.get(SubjectKind.PERSONA, _ALREADY) == already, "its open erasure is kept"
        assert ledger.get(SubjectKind.PERSONA, _NOT_MINTED) is None
    finally:
        engine.dispose()
