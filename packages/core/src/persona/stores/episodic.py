"""Episodic store — runtime-writable; usage-reinforced decay ranking (Spec K8).

The flat-24h decay (D-01-4) is GONE — replaced by the K8-D-4 ranking:
``score = similarity · max(retention, ranking_floor)`` where retention is
MemoryBank's usage-reinforced ``R = exp(−Δt/(tau0·strength))`` from
:mod:`persona.stores.lifecycle` (Δt counts from the last recall). The floor
guarantees decay down-ranks but never rank-kills: old-but-relevant memory
stays findable (keep-all-embeddings makes it reachable; the floor makes it
rankable). K9 owns the final composite scoring; this is the interim
store-level ranking only.

Reinforcement (K8-D-5) is the EXPLICIT :meth:`EpisodicStore.reinforce`
command — MemoryBank's rule (``strength += 1``, clock reset), one batched
backend UPDATE per turn. It is called by the chat loop synchronously after
retrieval and by the voice path inside its already-off-loop retrieval thread
(the event-loop-starvation rule); ``retrieve_context`` itself stays pure (CQS).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from persona.audit import AuditAction
from persona.schema.chunks import PersonaChunk, WriteSource
from persona.stores.base import TypedStore
from persona.stores.lifecycle import EpisodicSettings, retention
from persona.stores.policy import PolicyDecision, PolicyRule, PolicyTable
from persona.stores.pyramid import EpisodicPyramid, resolve_display

__all__ = ["EpisodicStore"]


class EpisodicStore(TypedStore):
    """Runtime-writable; ranked by usage-reinforced retention at query time."""

    STORE_KIND: ClassVar[str] = "episodic"
    _POLICY: ClassVar[PolicyTable] = {
        WriteSource.SYSTEM: PolicyRule(decision=PolicyDecision.ACCEPT),
        WriteSource.USER: PolicyRule(decision=PolicyDecision.ACCEPT),
        WriteSource.PERSONA_SELF: PolicyRule(decision=PolicyDecision.ACCEPT),
    }

    def __init__(
        self,
        *,
        backend: Any,  # noqa: ANN401 — composes via the base
        audit_logger: Any,  # noqa: ANN401
        settings: EpisodicSettings | None = None,
    ) -> None:
        super().__init__(backend=backend, audit_logger=audit_logger)
        self._settings = settings if settings is not None else EpisodicSettings()
        # The pyramid surface over the SAME transport (Spec K8 T4): gist rows,
        # drill-down, the privacy cascade, and band-resolved display.
        self._pyramid = EpisodicPyramid(backend=backend, audit_logger=audit_logger)

    @property
    def settings(self) -> EpisodicSettings:
        return self._settings

    @property
    def pyramid(self) -> EpisodicPyramid:
        """The gist/drill surface (the engine writes it; K9 reads it)."""
        return self._pyramid

    def query(
        self,
        persona_id: str,
        query: str,
        top_k: int,
        **filters: Any,  # noqa: ANN401
    ) -> list[PersonaChunk]:
        # Pull more candidates than top_k so retention-reranking can pick a
        # different top set than the raw nearest neighbours. 3x is
        # conventional; bounded by the SQLite cap upstream.
        n_candidates = max(top_k * 3, top_k)
        candidates = super().query(persona_id, query, n_candidates, **filters)
        now = datetime.now(UTC)
        ranked = sorted(
            candidates,
            key=lambda c: self._ranking_score(c, now=now),
            reverse=True,
        )
        return ranked[:top_k]

    def _ranking_score(self, chunk: PersonaChunk, *, now: datetime) -> float:
        # Cosine distance -> similarity (1 - distance) for L2-normalised
        # embeddings, scaled by floored retention (K8-D-4): decay can
        # down-rank a memory but never rank-kill it.
        similarity = 0.0 if chunk.distance is None else 1.0 - float(chunk.distance)
        r = retention(chunk, now=now, settings=self._settings)
        return similarity * max(r, self._settings.ranking_floor)

    def resolve_display(self, persona_id: str, chunks: list[PersonaChunk]) -> list[PersonaChunk]:
        """Band-resolved display for recalled chunks (K8-D-11).

        Demoted hits (band 1) render as their covering gist prefixed with the
        exact, constant ``OLDER_MEMORY_MARKER``; FULL chunks and chunks with no
        covering gist pass through raw (fail-soft). The retrieval SET is
        unchanged — display fidelity only; stored rows are never mutated.
        """
        demoted = [c.id for c in chunks if c.band == 1 and not c.member_ids]
        if not demoted:
            return list(chunks)
        covering = self._pyramid.covering_gists(persona_id, demoted)
        return resolve_display(chunks, covering)

    # ----- delete (the K8-D-14 privacy cascade rides the sanctioned paths) ----

    def remove_documents(self, persona_id: str, doc_ids: list[str]) -> None:
        """True-delete raw chunks, then cascade to every intersecting gist.

        The only sanctioned raw-delete path besides :meth:`delete` (privacy /
        correction). A derived gist must not outlive its evidence (K8-D-14);
        survivors regenerate from originals on the next engine pass.
        """
        super().remove_documents(persona_id, doc_ids)
        if doc_ids:
            self._pyramid.remove_gists_for_members(persona_id, doc_ids)

    def delete(self, persona_id: str) -> None:
        """Wipe the persona's episodic store AND its gist layer (K8-D-14)."""
        super().delete(persona_id)
        self._pyramid.delete_all(persona_id)

    def reinforce(self, persona_id: str, chunk_ids: list[str]) -> None:
        """Reinforce recalled chunks: ``strength += 1``, decay clock reset (K8-D-5).

        MemoryBank's exact rule, batched — ONE backend UPDATE per turn for the
        whole recalled id-set (no per-chunk write amplification). Explicit
        command (CQS): retrieval stays pure; the turn paths call this after
        retrieval (chat: sync on the request engine; voice: inside its
        off-loop retrieval thread). Empty input is a no-op.
        """
        if not chunk_ids:
            return
        now = datetime.now(UTC)
        self._backend.reinforce(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            ids=list(chunk_ids),
            recalled_at=now,
        )
        self._emit_audit(
            persona_id=persona_id,
            action=AuditAction.REINFORCE,
            source=WriteSource.SYSTEM,
            written_by="episodic.reinforce",
            chunk_ids=list(chunk_ids),
        )
