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
