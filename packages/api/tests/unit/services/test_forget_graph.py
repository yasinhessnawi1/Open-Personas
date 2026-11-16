"""Forgetting a merged member: which of its source-less mirrors stay (R9-287).

The releaser's SQL runs in CI (tests/integration/test_forget_merged_member_postgres.py); this
pins its one decision of its own. The rest is the persona erasure's functions, unchanged.
"""

from __future__ import annotations

from persona_api.services.forget_graph import shared_legacy_mirrors


def test_a_source_less_mirror_another_record_lists_is_kept() -> None:
    record = {"run_id": "r1", "mirrored_edges": ["e1", "e2", "e3"], "mirror_sources": {"e3": [7]}}
    others = [
        {"run_id": "r1", "mirrored_edges": ["e1"]},  # another record lists e1 without a source
        {"run_id": "r1", "mirror_sources": {"e2": [9]}},  # and e2 with one
    ]

    assert shared_legacy_mirrors(record, others) == {"e1", "e2"}


def test_a_mirror_only_this_record_lists_goes() -> None:
    record = {"run_id": "r1", "mirrored_edges": ["e1"]}

    assert shared_legacy_mirrors(record, [{"run_id": "r1", "mirrored_edges": ["e9"]}]) == set()


def test_a_sourced_mirror_is_not_this_rules_business() -> None:
    """A mirror with sources goes by the all-sources rule after the delete, not by id."""
    record = {"run_id": "r1", "mirrored_edges": ["e3"], "mirror_sources": {"e3": [7]}}

    assert shared_legacy_mirrors(record, [{"run_id": "r1", "mirrored_edges": ["e3"]}]) == set()


def test_a_listing_from_another_run_keeps_nothing() -> None:
    """Review LOW-3: the same mirror id from another run names that run's row, not this one."""
    record = {"run_id": "r1", "mirrored_edges": ["e1"]}

    assert shared_legacy_mirrors(record, [{"run_id": "r2", "mirrored_edges": ["e1"]}]) == set()
    assert shared_legacy_mirrors(record, [{"run_id": "r2", "mirror_sources": {"e1": [3]}}]) == set()
