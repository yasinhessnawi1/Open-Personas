"""A merge act that stops part way is given back by the next run (R9-284 round 5 MEDIUM-1).

The record is written first, as "pending", and marked "done" only at the act's end. A failure in
between (here a simulated deadlock, SQLSTATE 40P01, after the record) leaves it pending; the next
run, first thing under the owner's lock, unmerges every pending member and then merges afresh.
A stateful fake backend holds the two nodes.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona.audit import MemoryAuditLogger
from persona.graph.config import GraphSettings
from persona.graph.consolidation import ConsolidationPass
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
_P = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _Deadlock(Exception):  # noqa: N818 - named for what it simulates
    sqlstate = "40P01"


def _node(node_id: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=(NodeProvenance(source=WriteSource.SYSTEM, persona_id=_P, written_at=_NOW),),
        created_at=_NOW,
    )


class _Backend:
    def __init__(
        self, *, deadlocks: int, broken_reopens: int = 0, broken_accretions: int = 0
    ) -> None:
        self.nodes = {"k": _node("k"), "m": _node("m")}
        self.merged: dict[str, str | None] = {"k": None, "m": None}
        self.events: list[str] = []
        self._deadlocks = deadlocks
        self._broken_reopens = broken_reopens
        self._broken_accretions = broken_accretions
        self.marker: tuple[int, datetime | None] = (0, None)
        self.disuse: list[int] = []

    @contextmanager
    def maintenance_lock(self, _owner: str) -> Iterator[None]:
        yield

    def pending_merges(self, _owner: str) -> list[str]:
        out = []
        for node in self.nodes.values():
            raw = node.metadata.get("_k7_merge")
            if self.merged[node.id] and raw and json.loads(raw).get("state") == "pending":
                out.append(node.id)
        return out

    def merge_records_listing(self, _owner: str, **_kw: Any) -> dict[str, str]:  # noqa: ANN401
        return {
            i: n.metadata["_k7_merge"]
            for i, n in self.nodes.items()
            if self.merged[i] and "_k7_merge" in n.metadata
        }

    def existing_edge_keys(self, _owner: str, keys: Any) -> set[int]:  # noqa: ANN401
        return set(keys)

    def get_marker(self, _owner: str) -> tuple[int, datetime | None]:
        return self.marker

    def dirty_candidates(self, _owner: str, _watermark: object) -> list[ConceptNode]:
        return [self.nodes["k"]]

    def band_semantic_neighbors(self, _owner: str, node_id: str, **_k: Any) -> list[ConceptNode]:  # noqa: ANN401
        return [n for n in self.nodes.values() if n.id != node_id and self.merged[n.id] is None]

    def get_embeddings(self, _owner: str, ids: Any) -> dict[str, list[float]]:  # noqa: ANN401
        return {i: [1.0, 0.0] for i in ids}

    def get_node(self, _owner: str, node_id: str) -> ConceptNode | None:
        return self.nodes.get(node_id)

    def open_assertion_edges_keyed(self, *_a: Any) -> list[Any]:  # noqa: ANN401
        return []

    def entities_for_node(self, *_a: Any) -> list[str]:  # noqa: ANN401
        return []

    def set_merged_into(self, _owner: str, node_id: str, **kw: Any) -> None:  # noqa: ANN401
        node = self.nodes[node_id]
        self.nodes[node_id] = node.model_copy(update={"metadata": kw["metadata"]})
        self.merged[node_id] = kw["merged_into"]
        state = json.loads(kw["metadata"]["_k7_merge"])["state"] if kw["merged_into"] else None
        self.events.append(f"record {node_id} {state or 'cleared'}")

    def close_edges_incident(self, _owner: str, node_id: str, **_k: Any) -> list[str]:  # noqa: ANN401
        if self._deadlocks:
            self._deadlocks -= 1
            raise _Deadlock("deadlock detected")
        self.events.append(f"close {node_id}")
        return []

    def reopen_edges_incident(self, _owner: str, node_id: str, **_k: Any) -> None:  # noqa: ANN401
        if self._broken_reopens:
            self._broken_reopens -= 1
            raise _Deadlock("the connection went too")
        self.events.append(f"reopen {node_id}")

    def associate_entities_recording(
        self,
        owner: str,
        _node: str,
        _ids: Any,  # noqa: ANN401
        **kw: Any,  # noqa: ANN401
    ) -> list[str]:
        metadata, content_hash = kw["record"]([])
        self.set_merged_into(
            owner,
            kw["member_id"],
            merged_into=kw["merged_into"],
            metadata=metadata,
            content_hash=content_hash,
        )
        return []

    def remove_entity_associations(self, *_a: Any) -> None:  # noqa: ANN401
        return None

    def delete_open_mirror(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        return None

    def surrogate_for(self, *_a: Any) -> None:  # noqa: ANN401
        return None

    def bump_salience(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        return None

    def update_node(self, _owner: str, node: ConceptNode, *_a: Any, **_k: Any) -> int:  # noqa: ANN401
        if self._broken_accretions:
            self._broken_accretions -= 1
            raise _Deadlock("the alias accretion deadlocked")
        self.nodes[node.id] = node
        return 1

    def apply_disuse_decay(self, *_a: Any, **kw: Any) -> None:  # noqa: ANN401
        self.disuse.append(kw["current_epoch"])

    def advance_marker(self, _owner: str, **kw: Any) -> None:  # noqa: ANN401
        self.marker = (kw["epoch"], kw["watermark"])


def _pass(backend: _Backend) -> ConsolidationPass:
    return ConsolidationPass(
        backend=backend,  # type: ignore[arg-type]
        index=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )


def _state(backend: _Backend) -> str | None:
    raw = backend.nodes["m"].metadata.get("_k7_merge")
    return None if raw is None else str(json.loads(raw)["state"])


def test_a_failed_cluster_is_given_back_in_the_same_run() -> None:
    """CI on a8ea528a: a failing cluster must leave its members unmerged (the K7 contract)."""
    backend = _Backend(deadlocks=1)

    report = _pass(backend).run("u1")

    assert report.clusters_formed == 0
    assert backend.events == ["record m pending", "reopen m", "record m cleared"]
    assert backend.merged["m"] is None


def test_a_member_the_run_could_not_give_back_is_given_back_by_the_next_run() -> None:
    """A crash, or a give back that failed too, leaves it pending for the next run."""
    backend = _Backend(deadlocks=1, broken_reopens=1)
    consolidation = _pass(backend)

    first = consolidation.run("u1")

    assert first.clusters_formed == 0
    assert backend.merged["m"] == "k"
    assert _state(backend) == "pending"

    backend.events.clear()
    second = consolidation.run("u1")

    assert backend.events[:2] == ["reopen m", "record m cleared"], "given back first"
    assert second.clusters_formed == 1
    assert backend.merged["m"] == "k"
    assert _state(backend) == "done"
    assert backend.events[2:] == ["record m pending", "close m", "record m done"]


def test_a_done_record_and_one_without_a_state_are_left_alone() -> None:
    backend = _Backend(deadlocks=0)
    _pass(backend).run("u1")
    assert _state(backend) == "done"
    legacy = {"run_id": "u1:0", "canonical": "k", "mirrored_edges": [], "added_entities": []}
    node = backend.nodes["m"]
    backend.nodes["m"] = node.model_copy(update={"metadata": {"_k7_merge": json.dumps(legacy)}})

    assert backend.pending_merges("u1") == []


def test_one_pending_member_that_cannot_be_given_back_does_not_stop_the_others() -> None:
    """Round 6 L-1: each pending member is given back on its own."""
    backend = _Backend(deadlocks=1, broken_reopens=1)
    consolidation = _pass(backend)
    consolidation.run("u1")
    assert _state(backend) == "pending"
    real_pending, real_get = backend.pending_merges, backend.get_node

    def _get(owner: str, node_id: str) -> ConceptNode | None:
        if node_id == "ghost":
            raise RuntimeError("cannot read it")
        return real_get(owner, node_id)

    backend.pending_merges = lambda owner: ["ghost", *real_pending(owner)]  # type: ignore[method-assign]
    backend.get_node = _get  # type: ignore[method-assign]
    backend.events.clear()

    try:
        report = consolidation.run("u1")
    except RuntimeError as exc:
        raise AssertionError(f"one failed give back stopped the run: {exc}") from None

    assert backend.events[:2] == ["reopen m", "record m cleared"], "m is given back anyway"
    assert report.clusters_formed == 0, "round 7 MEDIUM-1: no merge this run"


def test_a_failed_give_back_means_no_merge_that_run() -> None:
    """Round 7 MEDIUM-1: the next run, with nothing stuck, merges again."""
    backend = _Backend(deadlocks=1, broken_reopens=2)
    consolidation = _pass(backend)
    consolidation.run("u1")  # merge fails, its give back fails: m pending
    backend.events.clear()

    stuck = consolidation.run("u1")  # the pending give back fails again

    assert stuck.clusters_formed == 0
    assert [c.node_id for c in stuck.skipped] == ["m"]
    assert backend.events == ["record m pending"], "nothing merged; the failure is counted"

    backend.events.clear()
    healthy = consolidation.run("u1")

    assert backend.events[:2] == ["reopen m", "record m cleared"]
    assert healthy.clusters_formed == 1


def test_a_done_member_is_marked_pending_before_its_give_back() -> None:
    """Round 6 L-2: the act finished, the cluster then failed, and the give back failed too."""
    backend = _Backend(deadlocks=0, broken_reopens=1, broken_accretions=1)

    _pass(backend).run("u1")

    assert backend.events[-3:] == ["close m", "record m done", "record m pending"]
    assert _state(backend) == "pending", "so the next run gives it back"


def test_a_run_that_skips_its_merges_still_decays_and_keeps_its_watermark() -> None:
    """Round 8 MEDIUM-A (a): disuse decays at a new epoch; the candidates stay dirty."""
    backend = _Backend(deadlocks=1, broken_reopens=2)
    consolidation = _pass(backend)
    consolidation.run("u1")  # m left pending; the marker now has a watermark
    epoch, watermark = backend.marker
    assert watermark is not None
    backend.disuse.clear()

    report = consolidation.run("u1")  # the give back fails: merges skipped

    assert report.clusters_formed == 0
    assert backend.disuse, "disuse ran"
    assert set(backend.disuse) == {epoch + 1}
    assert backend.marker == (epoch + 1, watermark), "a new epoch, the same watermark"


class _Clock(datetime):
    now_at = _NOW

    @classmethod
    def now(cls, _tz: object = None) -> datetime:  # type: ignore[override]
        return cls.now_at


def test_a_give_back_stuck_five_times_alerts_once_then_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 MEDIUM-A (b): the count lives in the pending record."""
    from datetime import timedelta  # noqa: PLC0415

    from persona.graph import consolidation as module  # noqa: PLC0415

    alerts: list[str] = []
    monkeypatch.setattr(module._log, "error", lambda msg, **_k: alerts.append(msg))  # noqa: SLF001
    monkeypatch.setattr(module, "datetime", _Clock)
    backend = _Backend(deadlocks=1, broken_reopens=100)
    consolidation = _pass(backend)
    consolidation.run("u1")  # m pending (its in-run give back failed too)

    for run in range(1, 7):
        _Clock.now_at = _NOW + timedelta(minutes=run)
        consolidation.run("u1")
        record = json.loads(backend.nodes["m"].metadata["_k7_merge"])
        assert record["give_back_failures"] == run
    assert len(alerts) == 1, "the fifth failure alerts, the sixth does not"
    assert alerts[0].startswith("OPERATOR ALERT: consolidation give back stuck")

    _Clock.now_at = _NOW + timedelta(hours=25)
    consolidation.run("u1")
    assert len(alerts) == 2, "again after a day"
