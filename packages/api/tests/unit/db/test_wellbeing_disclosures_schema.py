"""``wellbeing_disclosures``: the durable per-interaction disclosure fact (R9-253). No database.

The initiative wellbeing gate used to derive "this conversation carried a disclosure" from the
tagged nodes that exist now, so forgetting the tagged node un-gated its untagged siblings. The
fact lives in its own table, holds only what the gate needs (D-ORCH-11-18-m: no category, no
text), outlives the node and the conversation, and goes with its persona or its account.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from persona_api.db.community import build_community_metadata
from persona_api.db.models import wellbeing_disclosures

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "065_wellbeing_disclosures.py"
)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wellbeing_disclosures_065", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_holds_ids_and_a_time_and_nothing_else() -> None:
    """Data minimisation: no category, no content, no kind. The row's existence is the fact."""
    assert set(wellbeing_disclosures.columns.keys()) == {
        "owner_id",
        "persona_id",
        "interaction_id",
        "created_at",
    }


def test_one_fact_per_owner_and_interaction() -> None:
    assert [c.name for c in wellbeing_disclosures.primary_key.columns] == [
        "owner_id",
        "interaction_id",
    ]


def test_it_goes_with_its_account_and_with_its_persona() -> None:
    """The erasure leaves nothing behind: both parents cascade (D-R9-284-erase)."""
    parents = {
        column: [
            (fk.column.table.name, fk.ondelete)
            for fk in wellbeing_disclosures.c[column].foreign_keys
        ]
        for column in ("owner_id", "persona_id", "interaction_id")
    }
    assert parents == {
        "owner_id": [("users", "CASCADE")],
        "persona_id": [("personas", "CASCADE")],
        # No key to the conversation: the nodes learned from it outlive it, so the fact must.
        "interaction_id": [],
    }


def test_the_cascade_has_an_index_to_run_on() -> None:
    indexed = {tuple(c.name for c in index.columns) for index in wellbeing_disclosures.indexes}
    assert ("persona_id",) in indexed


def test_it_is_not_in_the_legacy_sqlite_build() -> None:
    """No graph there, so nothing tags and nothing reads it (the synthesis_markers precedent)."""
    assert "wellbeing_disclosures" not in build_community_metadata().tables


def test_the_migration_follows_the_account_erasure() -> None:
    module = _module()
    assert module.revision == "065_wellbeing_disclosures"
    assert module.down_revision == "064_account_erasure"


def test_the_migration_scopes_the_table_to_its_owner_for_reads_and_writes() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    module = _module()
    assert "USING (" in module.POLICY
    assert "WITH CHECK (" in module.POLICY
    assert "current_setting('app.current_user_id', true)" in module.POLICY


def test_the_backfill_reads_tagged_nodes_only_and_never_their_text() -> None:
    backfill = _module().BACKFILL
    assert "wellbeing_category IS NOT NULL" in backfill
    assert "interaction_id" in backfill
    assert "ON CONFLICT DO NOTHING" in backfill
    for content in ("content", "grounding", "concept_name"):
        assert content not in backfill
