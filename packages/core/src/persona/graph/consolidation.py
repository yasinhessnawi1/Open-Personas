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
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.audit import AuditLogger
    from persona.graph.config import GraphSettings
    from persona.stores.embedder import Embedder

__all__ = ["ConsolidationPass", "ConsolidationReport", "MergeGroup", "SkippedCandidate"]

#: Reserved member-metadata key holding the JSON reversal record (K7-D-4.5). Present
#: ⇔ the node is a consolidated member; unmerge reads it to invert precisely.
_MERGE_KEY = "_k7_merge"
#: Reserved canonical-metadata key holding the accreted alias list (K7-D-4.4).
_ALIAS_KEY = "_k7_aliases"


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
    def open_assertion_edges(self, owner_id: str, node_id: str) -> list[TypedLink]: ...
    def upsert_edge(self, owner_id: str, link: TypedLink) -> None: ...
    def close_edges_incident(
        self, owner_id: str, node_id: str, *, invalidated_by: str
    ) -> list[str]: ...
    def reopen_edges_incident(
        self, owner_id: str, node_id: str, *, invalidated_by: str
    ) -> None: ...
    def delete_open_edge(self, owner_id: str, edge_id: str) -> None: ...
    def entities_for_node(self, owner_id: str, node_id: str) -> list[str]: ...
    def associate_entities(
        self, owner_id: str, node_id: str, entity_ids: Sequence[str]
    ) -> None: ...
    def remove_entity_associations(
        self, owner_id: str, node_id: str, entity_ids: Sequence[str]
    ) -> None: ...
    def update_node(
        self, owner_id: str, node: ConceptNode, embedding: Sequence[float]
    ) -> int | None: ...
    def set_merged_into(
        self,
        owner_id: str,
        node_id: str,
        *,
        merged_into: str | None,
        metadata: dict[str, str],
        content_hash: str,
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
        """Consolidate the owner's dirty near-dup neighbourhoods → a report (K7-D-4)."""
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
                # must not abort the run; record it honestly and carry on (K7-D-4.6).
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
            mirrored = self._mirror_assertion_edges(owner_id, member, canonical, tag=tag, now=now)
            self._backend.close_edges_incident(owner_id, member.id, invalidated_by=tag)
            added = self._copy_entities(owner_id, member, canonical)
            self._mark_merged(
                owner_id, member, canonical, run_id=run_id, mirrored=mirrored, added=added
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
            owner_id, canonical.id, [m.concept_name for m in members], run_id=run_id, now=now
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

    def _mirror_assertion_edges(
        self, owner_id: str, member: ConceptNode, canonical: ConceptNode, *, tag: str, now: datetime
    ) -> list[str]:
        mirrored: list[str] = []
        for edge in self._backend.open_assertion_edges(owner_id, member.id):
            if edge.src_node_id == member.id:
                src, dst = canonical.id, edge.dst_node_id
            else:
                src, dst = edge.src_node_id, canonical.id
            if src == dst:  # a self-loop after re-pointing onto the canonical — skip
                continue
            mirror_id = make_edge_id(src, dst, edge.link_type)
            self._backend.upsert_edge(
                owner_id,
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
                ),
            )
            mirrored.append(mirror_id)
        return mirrored

    def _copy_entities(
        self, owner_id: str, member: ConceptNode, canonical: ConceptNode
    ) -> list[str]:
        canon = set(self._backend.entities_for_node(owner_id, canonical.id))
        added = [e for e in self._backend.entities_for_node(owner_id, member.id) if e not in canon]
        self._backend.associate_entities(owner_id, canonical.id, added)
        return added

    def _mark_merged(
        self,
        owner_id: str,
        member: ConceptNode,
        canonical: ConceptNode,
        *,
        run_id: str,
        mirrored: list[str],
        added: list[str],
    ) -> None:
        record = {
            "run_id": run_id,
            "canonical": canonical.id,
            "mirrored_edges": mirrored,
            "added_entities": added,
        }
        new_meta = {**member.metadata, _MERGE_KEY: json.dumps(record, sort_keys=True)}
        # content byte-untouched → content_hash recomputes over the new metadata only.
        self._backend.set_merged_into(
            owner_id,
            member.id,
            merged_into=canonical.id,
            metadata=new_meta,
            content_hash=_compute_node_hash(member.concept_name, member.content, new_meta),
        )

    def _accrete_aliases(
        self, owner_id: str, canonical_id: str, names: list[str], *, run_id: str, now: datetime
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
                    reason=f"consolidation:{run_id} aliases:{','.join(fresh)}",
                ),
            ),
            created_at=current.created_at,
        )
        self._backend.update_node(owner_id, rebuilt, emb)

    # ===== the inverse ====================================================

    def unmerge(self, owner_id: str, node_id: str) -> bool:
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

        for mirror_id in record.get("mirrored_edges", []):
            self._backend.delete_open_edge(owner_id, mirror_id)
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
