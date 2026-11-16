"""Migration 064 builds what the account erasure needs, and the model agrees (R9-284 T9)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from persona_api.db.models import erasures, retained_billing_records

_MIGRATION = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "064_account_erasure.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("account_erasure_064", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_the_graph_backfill() -> None:
    module = _module()
    assert module.revision == "064_account_erasure"
    assert module.down_revision == "063_graph_erasure_backfill"


def test_the_fence_index_covers_finished_erasures() -> None:
    (index,) = [i for i in erasures.indexes if i.name == "idx_erasures_subject"]
    assert [c.name for c in index.columns] == ["subject_kind", "subject_id"]
    assert not index.dialect_options["postgresql"]["where"], "finished rows must be indexed"


def test_the_retained_table_outlives_the_user_and_is_platform_only() -> None:
    assert not [c for c in retained_billing_records.columns if c.foreign_keys]
    assert "user_id" not in retained_billing_records.columns
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY" not in source, "no policy: persona_app reaches nothing"


def test_a_resumed_copy_cannot_add_a_row_twice() -> None:
    (unique,) = [i for i in retained_billing_records.indexes if i.unique]
    assert [c.name for c in unique.columns] == ["erasure_id", "source_table", "source_id"]


def _sqlite_with_tables(*, kept: bool) -> object:
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine("sqlite://")
    erasures.create(engine)
    with engine.begin() as conn:  # the real table needs Postgres; its name is what matters
        conn.execute(text("CREATE TABLE retained_billing_records (id TEXT PRIMARY KEY)"))
        if kept:
            conn.execute(text("INSERT INTO retained_billing_records (id) VALUES ('r1')"))
    return engine


def _downgrade_on(engine: object, monkeypatch: object) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    module = _module()
    with engine.begin() as conn:  # type: ignore[attr-defined]
        monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: conn))  # type: ignore[attr-defined]
        module.downgrade()


def test_the_downgrade_refuses_while_records_are_retained(monkeypatch: object) -> None:
    """Review L5: retained billing records are not lost to a downgrade."""
    import pytest  # noqa: PLC0415
    from sqlalchemy import inspect  # noqa: PLC0415

    engine = _sqlite_with_tables(kept=True)

    with pytest.raises(RuntimeError, match="refusing to downgrade 064"):
        _downgrade_on(engine, monkeypatch)

    assert inspect(engine).has_table("retained_billing_records"), "nothing was dropped"


def test_the_downgrade_drops_an_empty_table(monkeypatch: object) -> None:
    from sqlalchemy import inspect  # noqa: PLC0415

    engine = _sqlite_with_tables(kept=False)

    _downgrade_on(engine, monkeypatch)

    assert not inspect(engine).has_table("retained_billing_records")
