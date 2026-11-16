"""The background near-duplicate consolidation pass (Spec K7, T4 / K7-D-4).

Turns near-duplicate concept nodes (the [0.82, 0.88) semantic band, K7-D-3) into a
canonical + soft ``merged_into`` members — **additively, provenance-safely, reversibly,
and LLM-free** (§0). Deterministic, off the hot path, per owner:

- **Scope (K7-D-4.1):** dirty neighbourhoods only — non-merged, non-SELF nodes with
  ``updated_at`` past the per-owner watermark. Localized maintenance, never a global
  re-org (2606.24775).
- **Clustering (K7-D-4.2):** star/center, NOT connected components — seed = the
  highest-evidence unconsolidated candidate; members = the seed's open band-edge
  neighbours whose similarity **to the seed** (recomputed from stored embeddings)
  clears the band floor. Transitive chaining is structurally impossible.
- **Survivorship (K7-D-4.3):** the seed rule — most evidence (provenance-trail length),
  tie → oldest ``created_at``, tie → min ``id``. Deterministic ⇒ idempotent.
- **The merge act (K7-D-4.4, all additive per member):** mirror the member's OPEN
  assertion edges (temporal/causal, both directions) onto the canonical; window-close
  the member's edges (``invalidated_by='consolidation:{run_id}'``); copy the member's
  NEW entity associations to the canonical; accrete the member's ``concept_name`` into
  the canonical's alias list (metadata, provenance-appended — content byte-untouched);
  set ``merged_into`` + a reversal record; drop the member from the dense index.
  **Canonical and member ``content``/``embedding`` are byte-untouched, ever.**
- **Reversibility (K7-D-4.5):** :meth:`ConsolidationPass.unmerge` is the precise
  inverse — restores content, embedding, edge topology, entity associations, and
  search visibility to the pre-merge state (proven, not asserted).

This module imports NO chat/LLM backend — the guardrail test asserts it structurally.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from persona.audit import AuditAction, AuditEvent
from persona.graph.models import (
    ConceptNode,
    NodeKind,
    NodeProvenance,
    TypedLink,
    _compute_node_hash,
    make_edge_id,
)
from persona.graph.salience import SALIENCE_EXCLUDED_KINDS
from persona.logging import get_logger
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from persona.audit import AuditLogger
    from persona.graph.config import GraphSettings
    from persona.stores.embedder import Embedder

__all__ = ["ConsolidationPass", "ConsolidationReport", "MergeGroup", "SkippedCandidate"]

#: Reserved member-metadata key holding the JSON reversal record (K7-D-4.5). Present
#: ⇔ the node is a consolidated member; unmerge reads it to invert precisely.
_log = get_logger("graph.consolidation")

_MERGE_KEY = "_k7_merge"
#: Reserved canonical-metadata key holding the accreted alias list (K7-D-4.4).
_ALIAS_KEY = "_k7_aliases"

#: A merge record's state: the act is still running, or it finished (R9-284).
_PENDING = "pending"
_DONE = "done"


def _personas_of(node: ConceptNode) -> list[str]:
    """The personas a node's trail names, for the R9-284 write guard."""
    return sorted({p.persona_id for p in node.provenance if p.persona_id})


def _held(asserters: dict[str, str], mirror_id: str) -> list[str]:
    """The source's asserting persona for one mirror, if it has one."""
    return [asserters[mirror_id]] if mirror_id in asserters else []


class MergeGroup(BaseModel):
    """One consolidated cluster in a :class:`ConsolidationReport`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_id: str
    member_ids: tuple[str, ...]


class SkippedCandidate(BaseModel):
    """A candidate the pass considered but did NOT merge, with the reason (no silent skips)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    reason: str


class ConsolidationReport(BaseModel):
    """The observable outcome of one :meth:`ConsolidationPass.run` (K7-D-4.6).

    The observability surface: every candidate is accounted for — merged (in
    :attr:`merges`) or skipped-with-reason (in :attr:`skipped`); no silent skips.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    owner_id: str
    epoch: int
    candidates_considered: int
    clusters_formed: int
    nodes_merged: int
    merges: tuple[MergeGroup, ...] = ()
    skipped: tuple[SkippedCandidate, ...] = ()


class _ConsolidationBackend(Protocol):
    """The transport surface the pass composes (PostgresGraphBackend satisfies it)."""

    def maintenance_lock(self, owner_id: str) -> AbstractContextManager[None]: ...
    def pending_merges(self, owner_id: str) -> list[str]: ...
    def merge_records_listing(
        self, owner_id: str, *, run_id: str, mirror_ids: Sequence[str]
    ) -> dict[str, str]: ...
    def existing_edge_keys(self, owner_id: str, edge_keys: Sequence[int]) -> set[int]: ...
    def get_marker(self, owner_id: str) -> tuple[int, datetime | None]: ...
    def advance_marker(
        self, owner_id: str, *, epoch: int, watermark: datetime, last_run_at: datetime
    ) -> None: ...
    def dirty_candidates(self, owner_id: str, watermark: datetime | None) -> list[ConceptNode]: ...
    def band_semantic_neighbors(
        self, owner_id: str, node_id: str, *, floor: float, ceil: float
    ) -> list[ConceptNode]: ...
    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]: ...
    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None: ...
    def open_assertion_edges_keyed(
        self, owner_id: str, node_id: str
    ) -> list[tuple[TypedLink, int]]: ...
    def upsert_edge(
        self, owner_id: str, link: TypedLink, *, guard_personas: Sequence[str] = ()
    ) -> None: ...
    def close_edges_incident(
        self, owner_id: str, node_id: str, *, invalidated_by: str
    ) -> list[str]: ...
    def reopen_edges_incident(
        self, owner_id: str, node_id: str, *, invalidated_by: str
    ) -> None: ...
    def delete_open_edge(self, owner_id: str, edge_id: str) -> None: ...
    def insert_edge_if_absent(
        self, owner_id: str, link: TypedLink, *, guard_personas: Sequence[str] = ()
    ) -> bool: ...
    def delete_open_mirror(self, owner_id: str, edge_id: str, *, tag: str) -> None: ...
    def entities_for_node(self, owner_id: str, node_id: str) -> list[str]: ...
    def associate_entities_returning(
        self,
        owner_id: str,
        node_id: str,
        entity_ids: Sequence[str],
        *,
        guard_personas: Sequence[str] = (),
    ) -> list[str]: ...
    def remove_entity_associations(
        self, owner_id: str, node_id: str, entity_ids: Sequence[str]
    ) -> None: ...
    def update_node(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        guard_personas: Sequence[str] = (),
    ) -> int | None: ...
    def set_merged_into(
        self,
        owner_id: str,
        node_id: str,
        *,
        merged_into: str | None,
        metadata: dict[str, str],
        content_hash: str,
        guard_personas: Sequence[str] = (),
    ) -> None: ...
    def surrogate_for(self, owner_id: str, node_id: str) -> int | None: ...
    def bump_salience(
        self, owner_id: str, node_id: str, *, delta: float, epoch: int, floor: float, cap: float
    ) -> None: ...
    def apply_disuse_decay(
        self,
        owner_id: str,
        node_kind: str,
        *,
        current_epoch: int,
        grace: int,
        delta: float,
        floor: float,
    ) -> None: ...


class _ConsolidationIndex(Protocol):
    """The dense-index surface the pass touches (same-path visibility discipline)."""

    def add(self, *, surrogate: int, vector: Sequence[float]) -> None: ...
    def remove(self, surrogate: int) -> bool: ...


class ConsolidationPass:
    """Deterministic, LLM-free near-dup consolidation over the transport + index (K7-D-4).

    Runnable standalone (``run(owner_id) -> ConsolidationReport``) — the K8-steerable
    seam (K7-D-5): K7 owns what the pass does, K8 will own when it runs.
    """

    def __init__(
        self,
        *,
        backend: _ConsolidationBackend,
        index: _ConsolidationIndex,
        embedder: Embedder,  # noqa: ARG002 — reserved for future re-embed-free hooks
        audit_logger: AuditLogger,
        settings: GraphSettings | None = None,
    ) -> None:
        from persona.graph.config import GraphSettings as _Settings

        self._backend = backend
        self._index = index
        self._audit = audit_logger
        self._settings = settings or _Settings()

    # ===== the pass =======================================================

    def run(self, owner_id: str) -> ConsolidationReport:
        """Consolidate the owner's dirty near-dup neighbourhoods → a report (K7-D-4).

        The whole run holds the owner's graph maintenance lock (R9-284,
        D-R9-284-graph-mutex), so an erasure's graph step never lands between two of its
        transactions; every read, the marker and the candidates first, happens under it.
        """
        with self._backend.maintenance_lock(owner_id):
            return self._run_locked(owner_id)

    def _run_locked(self, owner_id: str) -> ConsolidationReport:
        # A member whose merge act stopped part way (its record still "pending") is given
        # back first, so this run starts from whole merges only (D-R9-284-graph-mutex).
        for node_id in self._backend.pending_merges(owner_id):
            try:
                self._unmerge(owner_id, node_id)
            except Exception as exc:  # noqa: BLE001 - still pending: the next run tries again
                # One member's failure must not stop the others or the run (round 6 L-1).
                _log.warning(
                    "pending merge not given back node={node} error={err}",
                    node=node_id,
                    err=type(exc).__name__,
                )
        epoch, watermark = self._backend.get_marker(owner_id)
        new_epoch = epoch + 1
        run_id = f"{owner_id}:{new_epoch}"  # deterministic + unique; NOT a clock/random
        started_at = datetime.now(UTC)  # a SYSTEM timestamp (watermark axis) — never salience

        floor = self._settings.semantic_link_threshold
        ceil = self._settings.merge_extend_threshold
        min_size = self._settings.consolidation_min_cluster_size
        max_clusters = self._settings.consolidation_max_clusters_per_run
        max_members = self._settings.consolidation_max_members_per_cluster

        candidates = self._backend.dirty_candidates(owner_id, watermark)
        seen: set[str] = set()
        merges: list[MergeGroup] = []
        skipped: list[SkippedCandidate] = []

        for seed in candidates:
            if seed.id in seen:
                continue
            seen.add(seed.id)
            if len(merges) >= max_clusters:
                skipped.append(SkippedCandidate(node_id=seed.id, reason="run cluster cap reached"))
                continue
            members = self._cluster_members(owner_id, seed, floor=floor, ceil=ceil, seen=seen)
            if 1 + len(members) < min_size:
                skipped.append(
                    SkippedCandidate(node_id=seed.id, reason="no band members clear the floor")
                )
                continue
            members = members[:max_members]
            for member in members:
                seen.add(member.id)
            try:
                self._merge_cluster(
                    owner_id, seed, members, run_id=run_id, now=started_at, epoch=new_epoch
                )
            except Exception as exc:  # noqa: BLE001 — per-cluster isolation: one failure
                # must not abort the run; record it honestly and carry on (K7-D-4.6). The
                # cluster's members this run merged are given back, still under the lock, so
                # a failed cluster leaves none of them merged (R9-284 round 5 MEDIUM-1).
                self._give_back_cluster(owner_id, members, run_id=run_id)
                skipped.append(
                    SkippedCandidate(
                        node_id=seed.id, reason=f"merge failed: {type(exc).__name__}: {exc}"
                    )
                )
                continue
            merges.append(MergeGroup(canonical_id=seed.id, member_ids=tuple(m.id for m in members)))

        # Disuse decay (K7-D-6): every kind idle beyond its per-kind grace loses one
        # step this pass. TRAIT (δ_d = 0) and SELF never fade by disuse. Ordinal
        # epochs only — no clock. Runs once at the pass's new epoch.
        self._apply_disuse(owner_id, new_epoch)

        self._backend.advance_marker(
            owner_id, epoch=new_epoch, watermark=started_at, last_run_at=started_at
        )
        report = ConsolidationReport(
            run_id=run_id,
            owner_id=owner_id,
            epoch=new_epoch,
            candidates_considered=len(candidates),
            clusters_formed=len(merges),
            nodes_merged=sum(len(g.member_ids) for g in merges),
            merges=tuple(merges),
            skipped=tuple(skipped),
        )
        self._emit(
            owner_id,
            node_id=owner_id,
            metadata={
                "action": "consolidation_run",
                "run_id": run_id,
                "clusters": str(report.clusters_formed),
                "merged": str(report.nodes_merged),
            },
        )
        return report

    def _give_back_cluster(
        self, owner_id: str, members: Sequence[ConceptNode], *, run_id: str
    ) -> None:
        """Unmerge each member this run merged; one that fails is left for the next run.

        A member left "pending" by a failure here (or by a crash, where this never ran) is what
        the next run gives back first.
        """
        for member in members:
            current = self._backend.get_node(owner_id, member.id)
            raw = current.metadata.get(_MERGE_KEY) if current is not None else None
            try:
                record = json.loads(raw) if raw is not None else None
            except ValueError:
                record = None
            if not isinstance(record, dict) or record.get("run_id") != run_id:
                continue
            try:
                if current is not None and record.get("state") != _PENDING:
                    # A done member is marked pending first, so a give back that fails part
                    # way is retried by the next run instead of left half done (round 6 L-2).
                    self._mark_pending(owner_id, current, record)
                self._unmerge(owner_id, member.id)
            except Exception:  # noqa: BLE001, S112 - still pending: the next run gives it back
                continue

    def _mark_pending(self, owner_id: str, current: ConceptNode, record: dict[str, object]) -> None:
        canonical = record.get("canonical")
        if not isinstance(canonical, str):
            return
        metadata = {
            **current.metadata,
            _MERGE_KEY: json.dumps({**record, "state": _PENDING}, sort_keys=True),
        }
        self._backend.set_merged_into(
            owner_id,
            current.id,
            merged_into=canonical,
            metadata=metadata,
            content_hash=_compute_node_hash(current.concept_name, current.content, metadata),
        )

    def _cluster_members(
        self, owner_id: str, seed: ConceptNode, *, floor: float, ceil: float, seen: set[str]
    ) -> list[ConceptNode]:
        neighbours = self._backend.band_semantic_neighbors(
            owner_id, seed.id, floor=floor, ceil=ceil
        )
        neighbours = [n for n in neighbours if n.id not in seen and n.id != seed.id]
        if not neighbours:
            return []
        embs = self._backend.get_embeddings(owner_id, [seed.id, *(n.id for n in neighbours)])
        seed_emb = embs.get(seed.id)
        if seed_emb is None:
            return []
        # star-clustering: members must clear the floor against the SEED (not a chained
        # member) — recomputed exactly from stored embeddings (K7-D-4.2).
        return [
            n
            for n in neighbours
            if (n_emb := embs.get(n.id)) is not None and _cosine(seed_emb, n_emb) >= floor
        ]

    # ===== the merge act (additive, per member) ============================

    def _merge_cluster(
        self,
        owner_id: str,
        canonical: ConceptNode,
        members: list[ConceptNode],
        *,
        run_id: str,
        now: datetime,
        epoch: int,
    ) -> None:
        tag = f"consolidation:{run_id}"
        for member in members:
            personas = _personas_of(member)
            mirrors, sources, asserters = self._plan_mirrors(
                owner_id, member, canonical, tag=tag, now=now
            )
            to_copy = self._entities_to_copy(owner_id, member, canonical)
            # R9-284 (D-R9-284-write-guard): the merge record first, in a transaction that holds
            # the member's personas, listing the mirrors this merge will make. Its entity links
            # are recorded only once made, and only those the association added (round 6 L-4):
            # a pending record lists none, so a give back removes none.
            # An erasure landing at any later point finds them through the record; one that has
            # already landed refuses this write, and nothing below happens.
            self._mark_merged(
                owner_id,
                member,
                canonical,
                run_id=run_id,
                mirrored=[m.id for m in mirrors],
                added=[],
                # The record lists knowledge of the sources' asserters too (review round 5 H1).
                personas=sorted({*personas, *asserters.values()}),
                sources=sources,
                state=_PENDING,
            )
            # A mirror never overwrites an edge the canonical already has open (review
            # MEDIUM-1); the record then keeps only the mirrors actually made.
            made = [
                m.id
                for m in mirrors
                if self._backend.insert_edge_if_absent(
                    owner_id, m, guard_personas=sorted({*personas, *_held(asserters, m.id)})
                )
            ]
            self._backend.close_edges_incident(owner_id, member.id, invalidated_by=tag)
            added = self._backend.associate_entities_returning(
                owner_id, canonical.id, to_copy, guard_personas=personas
            )
            # The act is whole: the record says so, listing only the mirrors made. Until this
            # write it stays "pending", and the next run gives the member back.
            self._mark_merged(
                owner_id,
                member,
                canonical,
                run_id=run_id,
                mirrored=made,
                added=added,
                personas=sorted({*personas, *asserters.values()}),
                sources={m: sources[m] for m in made if m in sources},
                state=_DONE,
            )
            surrogate = self._backend.surrogate_for(owner_id, member.id)
            if surrogate is not None:
                self._index.remove(surrogate)  # leave the dense index (visibility)
            # Absorbing a near-dup INTO the canonical is corroboration (K7-D-6): the
            # canonical gains evidence-salience, stamped at this pass's epoch.
            self._backend.bump_salience(
                owner_id,
                canonical.id,
                delta=self._settings.salience_delta_corroboration,
                epoch=epoch,
                floor=self._settings.salience_floor,
                cap=self._settings.salience_cap,
            )
            self._emit(
                owner_id,
                node_id=canonical.id,
                metadata={"action": "consolidation_merge", "member": member.id, "run_id": run_id},
            )
        self._accrete_aliases(
            owner_id,
            canonical.id,
            [m.concept_name for m in members],
            run_id=run_id,
            now=now,
            personas=[p for m in members for p in _personas_of(m)],
        )

    def _apply_disuse(self, owner_id: str, current_epoch: int) -> None:
        """Decay every kind idle beyond its per-kind grace by one step (K7-D-6)."""
        for kind in NodeKind:
            if kind in SALIENCE_EXCLUDED_KINDS:
                continue
            delta = self._settings.disuse_delta_for(str(kind))
            if delta <= 0.0:  # TRAIT (and any kind configured to never fade)
                continue
            self._backend.apply_disuse_decay(
                owner_id,
                str(kind),
                current_epoch=current_epoch,
                grace=self._settings.disuse_grace_for(str(kind)),
                delta=delta,
                floor=self._settings.salience_floor,
            )

    def _plan_mirrors(
        self, owner_id: str, member: ConceptNode, canonical: ConceptNode, *, tag: str, now: datetime
    ) -> tuple[list[TypedLink], dict[str, list[int]], dict[str, str]]:
        """The mirrors of the member's open assertion edges onto the canonical, not yet written.

        Also ``{mirror id: [the source edge's edge_key]}``, so an erasure of the persona that
        asserted the source can find its mirror by row, not by recomputing ids (R9-284 LOW-3);
        a list, because members of one run can share a mirror (round 5 MEDIUM-2),
        and ``{mirror id: the source's asserting persona}``, held when the mirror is written.
        """
        mirrors: list[TypedLink] = []
        sources: dict[str, list[int]] = {}
        asserters: dict[str, str] = {}
        for edge, edge_key in self._backend.open_assertion_edges_keyed(owner_id, member.id):
            if edge.src_node_id == member.id:
                src, dst = canonical.id, edge.dst_node_id
            else:
                src, dst = edge.src_node_id, canonical.id
            if src == dst:  # a self-loop after re-pointing onto the canonical — skip
                continue
            mirror_id = make_edge_id(src, dst, edge.link_type)
            sources[mirror_id] = [edge_key]
            if edge.provenance is not None and edge.provenance.persona_id:
                asserters[mirror_id] = edge.provenance.persona_id
            mirrors.append(
                TypedLink(
                    id=mirror_id,
                    src_node_id=src,
                    dst_node_id=dst,
                    link_type=edge.link_type,
                    weight=edge.weight,
                    provenance=NodeProvenance(
                        source=WriteSource.SYSTEM, written_at=now, reason=tag
                    ),
                    created_at=now,
                    valid_at=now,
                )
            )
        return mirrors, sources, asserters

    def _entities_to_copy(
        self, owner_id: str, member: ConceptNode, canonical: ConceptNode
    ) -> list[str]:
        """The member's entity links the canonical does not have yet, not yet written."""
        canon = set(self._backend.entities_for_node(owner_id, canonical.id))
        return [e for e in self._backend.entities_for_node(owner_id, member.id) if e not in canon]

    def _mark_merged(
        self,
        owner_id: str,
        member: ConceptNode,
        canonical: ConceptNode,
        *,
        run_id: str,
        mirrored: list[str],
        added: list[str],
        personas: Sequence[str] = (),
        sources: dict[str, list[int]] | None = None,
        state: str = _DONE,
    ) -> None:
        record = {
            # "pending" while the member's merge act runs, "done" at its end; a record from
            # before this has no state and is done (D-R9-284-graph-mutex).
            "state": state,
            "run_id": run_id,
            "canonical": canonical.id,
            "mirrored_edges": mirrored,
            "added_entities": added,
            # {mirror id: [source edge_key]} (R9-284 LOW-3, round 5 MEDIUM-2): this member's
            # sources of each mirror it made or shares. Absent in records before LOW-3; a
            # single key in records before MEDIUM-2.
            "mirror_sources": {m: list(k) for m, k in (sources or {}).items()},
        }
        new_meta = {**member.metadata, _MERGE_KEY: json.dumps(record, sort_keys=True)}
        # content byte-untouched → content_hash recomputes over the new metadata only.
        self._backend.set_merged_into(
            owner_id,
            member.id,
            merged_into=canonical.id,
            metadata=new_meta,
            content_hash=_compute_node_hash(member.concept_name, member.content, new_meta),
            guard_personas=personas,
        )

    def _accrete_aliases(
        self,
        owner_id: str,
        canonical_id: str,
        names: list[str],
        *,
        run_id: str,
        now: datetime,
        personas: Sequence[str] = (),
    ) -> None:
        current = self._backend.get_node(owner_id, canonical_id)
        if current is None:  # pragma: no cover - the canonical was just read
            return
        aliases: list[str] = json.loads(current.metadata.get(_ALIAS_KEY, "[]"))
        fresh = [n for n in names if n and n != current.concept_name and n not in aliases]
        if not fresh:
            return  # idempotent: nothing new to accrete
        aliases.extend(fresh)
        new_meta = {**current.metadata, _ALIAS_KEY: json.dumps(aliases, sort_keys=True)}
        emb = self._backend.get_embeddings(owner_id, [canonical_id]).get(canonical_id)
        if emb is None:  # pragma: no cover - defensive
            return
        # content byte-untouched; embedding re-used (NOT re-embedded) — the canonical's
        # meaning is unchanged, only its alias bookkeeping grows (provenance-appended).
        # Constructed fresh so content_hash auto-recomputes over the new metadata.
        rebuilt = ConceptNode(
            id=current.id,
            node_kind=current.node_kind,
            concept_name=current.concept_name,
            content=current.content,
            metadata=new_meta,
            wellbeing_category=current.wellbeing_category,
            provenance=(
                *current.provenance,
                NodeProvenance(
                    source=WriteSource.SYSTEM,
                    written_at=now,
                    # The names as a JSON list: a name may hold a comma (R9-284 HIGH-3).
                    reason=f"consolidation:{run_id} aliases:{json.dumps(fresh)}",
                ),
            ),
            created_at=current.created_at,
        )
        # R9-284: the aliases are the members' names; hold the personas that gave them.
        self._backend.update_node(owner_id, rebuilt, emb, guard_personas=personas)

    # ===== the inverse ====================================================

    def unmerge(self, owner_id: str, node_id: str) -> bool:
        """Reverse a consolidated member, holding the owner's graph maintenance lock (R9-284)."""
        with self._backend.maintenance_lock(owner_id):
            return self._unmerge(owner_id, node_id)

    def _unmerge(self, owner_id: str, node_id: str) -> bool:
        """Reverse a consolidated member back to its pre-merge state (K7-D-4.5).

        The precise inverse: drop the mirrored edges from the canonical, remove the
        copied entity associations, re-open the member's window-closed edges, clear
        ``merged_into`` (restoring content_hash), and re-add the member to the dense
        index. Restores content + embedding + edge topology + entity associations +
        search visibility byte-exact. Returns ``False`` if the node is not a merged
        member. (The canonical keeps its accreted alias + provenance trace — append-only
        history, K7-D-4.4; retrieval is unaffected.)
        """
        member = self._backend.get_node(owner_id, node_id)
        if member is None:
            return False
        raw = member.metadata.get(_MERGE_KEY)
        if raw is None:
            return False  # not a consolidated member
        record = json.loads(raw)
        run_id = record["run_id"]
        tag = f"consolidation:{run_id}"

        shared = self._mirrors_shared_with(owner_id, node_id, record)
        for mirror_id in record.get("mirrored_edges", []):
            # Only while it is still this merge's mirror: the record may list one that was
            # never made, if the merge stopped before narrowing it (R9-284 MEDIUM-1). A mirror
            # another member of the same run still shares stays (round 5 MEDIUM-2).
            if mirror_id not in shared:
                self._backend.delete_open_mirror(owner_id, mirror_id, tag=tag)
        self._backend.remove_entity_associations(
            owner_id, record["canonical"], record.get("added_entities", [])
        )
        self._backend.reopen_edges_incident(owner_id, node_id, invalidated_by=tag)

        orig_meta = {k: v for k, v in member.metadata.items() if k != _MERGE_KEY}
        self._backend.set_merged_into(
            owner_id,
            node_id,
            merged_into=None,
            metadata=orig_meta,
            content_hash=_compute_node_hash(member.concept_name, member.content, orig_meta),
        )
        surrogate = self._backend.surrogate_for(owner_id, node_id)
        emb = self._backend.get_embeddings(owner_id, [node_id]).get(node_id)
        if surrogate is not None and emb is not None:
            self._index.add(surrogate=surrogate, vector=emb)
        self._emit(
            owner_id,
            node_id=node_id,
            metadata={"action": "consolidation_unmerge", "canonical": record["canonical"]},
        )
        return True

    def _mirrors_shared_with(
        self, owner_id: str, node_id: str, record: dict[str, object]
    ) -> set[str]:
        """Mirrors another member of the same run, still merged, also backs.

        Any member of the run, merged into any canonical (round 6 M-2): members of two
        clusters can make the same mirror id, A1 into C1 and B2 into C2 both C2::t::C1.

        It backs one only while one of the sources it lists for it is still a row (round 6
        M-1): a stale listing, whose source an erasure or a forget removed, keeps nothing. A
        mirror it lists without sources (a record before LOW-3) is taken as backed.
        """
        listed = record.get("mirrored_edges")
        mirror_ids = sorted({str(m) for m in listed}) if isinstance(listed, list) else []
        if not mirror_ids:
            return set()
        claims: dict[str, set[int]] = {}
        unsourced: set[str] = set()
        listing = self._backend.merge_records_listing(
            owner_id, run_id=str(record.get("run_id")), mirror_ids=mirror_ids
        )
        for other_id, raw in listing.items():
            if other_id == node_id:
                continue
            try:
                other = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(other, dict) or other.get("run_id") != record.get("run_id"):
                continue
            sources = other.get("mirror_sources") or {}
            for mirror in map(str, other.get("mirrored_edges", [])):
                if mirror not in mirror_ids:
                    continue
                if mirror not in sources:
                    unsourced.add(mirror)  # no sources listed: taken as backed
                    continue
                listed = sources[mirror]
                keys = listed if isinstance(listed, list) else [listed]
                claims.setdefault(mirror, set()).update(k for k in keys if isinstance(k, int))
        wanted = sorted({k for keys in claims.values() for k in keys})
        alive = self._backend.existing_edge_keys(owner_id, wanted) if wanted else set()
        return unsourced | {mirror for mirror, keys in claims.items() if keys & alive}

    # ===== helpers ========================================================

    def _emit(self, owner_id: str, *, node_id: str, metadata: dict[str, str]) -> None:
        self._audit.emit(
            AuditEvent(
                timestamp=datetime.now(UTC),
                persona_id=owner_id,  # the scope id (CSA-1)
                action=AuditAction.WRITE,
                store="knowledge_graph",
                source=WriteSource.SYSTEM,
                chunk_ids=[node_id],
                metadata=metadata,
            )
        )


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
