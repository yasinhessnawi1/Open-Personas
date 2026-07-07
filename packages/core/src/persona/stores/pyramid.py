"""The episodic pyramid surface — gists, drill-down, and the privacy cascade (Spec K8 T4).

The depth-2 pyramid (K8-D-1/2): raw chunks at the base (100% text + embedding,
kept forever), gist rows above (``store_kind='episodic_gist'``, ordinary
PersonaChunks whose ``member_ids`` point DOWN at their raw members). The
centerpiece invariant (§0): **drill-down always bottoms out at the untouched
100% original** — a gist is a derived artifact that points at evidence, never
a replacement for it. Structurally enforced here:

- ``write_gist`` touches ONLY the gist kind — it cannot mutate or delete a raw
  chunk (the write path never addresses the ``episodic`` kind at all), and it
  refuses a gist as a member (``GistMembershipError`` — from-originals is
  structural: no summary-of-summary can even be *stored*).
- Gist embeddings are ADDED (a new row embeds the gist text); a raw chunk's
  embedding is never substituted (different rows; the raw row is never
  re-upserted by any pyramid path).
- Gist identity is deterministic — ``uuid5`` over the sorted member-id set
  (K8-D-8) — so regeneration is idempotent: same members ⇒ same id ⇒ the
  upsert replaces rather than duplicates.
- The privacy cascade (K8-D-14): deleting raw chunks REMOVES every intersecting
  gist (a derived artifact must not outlive its evidence); survivors regenerate
  from originals on the next engine pass.

True-delete of raw chunks lives on :class:`persona.stores.episodic.EpisodicStore`
(the explicit user/privacy/correction path) — this module deletes ONLY gists.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from persona.audit import AuditAction, AuditEvent
from persona.errors import GistMembershipError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from persona.audit import AuditLogger, StoreKind
    from persona.stores.backend import Backend

__all__ = ["GIST_KIND", "OLDER_MEMORY_MARKER", "EpisodicPyramid", "make_gist_id"]

#: The Backend store_kind gist rows live under (K8-D-2; kind CHECK widened by
#: the K8 migration).
GIST_KIND: str = "episodic_gist"

#: The EXACT, CONSTANT demoted-display marker (K8-D-11 gate bar): the model
#: must be able to rely on this literal — never rephrase it.
OLDER_MEMORY_MARKER: str = "[older memory — summarized]"

#: Deterministic uuid5 namespace for gist ids (K8-D-8). Fixed forever — the
#: idempotent-regeneration property depends on it.
_GIST_NAMESPACE = uuid.UUID("8f4e2a10-9c3d-4b6e-8a71-2d5f0c9b1e47")


def make_gist_id(persona_id: str, member_ids: Sequence[str]) -> str:
    """The deterministic gist id: ``uuid5`` over the sorted member-id set.

    Same members ⇒ same id on every regeneration (idempotent upsert); any
    membership change ⇒ a new identity (the old gist is deleted by the pass
    that rebuilds the window, never mutated in place).
    """
    digest_input = "\n".join(sorted(member_ids))
    return f"{persona_id}::{GIST_KIND}::{uuid.uuid5(_GIST_NAMESPACE, digest_input)}"


class EpisodicPyramid:
    """Gist write/read/drill/cascade over the episodic transports.

    Composes the SAME injected :class:`Backend` the episodic store uses —
    gists ride both transports (Postgres columns / Chroma metadata) as
    ordinary chunks. Thin by design: no policy table (gists are SYSTEM-derived
    artifacts, not user memory writes) — validation is membership-structural.
    """

    _STORE: ClassVar[StoreKind] = "episodic_gist"

    def __init__(
        self,
        *,
        backend: Backend,
        audit_logger: AuditLogger,
    ) -> None:
        self._backend = backend
        self._audit = audit_logger

    # ----- write ------------------------------------------------------------

    def write_gist(
        self,
        persona_id: str,
        *,
        text: str,
        member_ids: Sequence[str],
        created_at: datetime,
        written_by: str = "episodic.engine",
    ) -> PersonaChunk:
        """Store one gist over ``member_ids`` (ordered) and return it.

        Raises:
            GistMembershipError: empty membership, or a member that is itself
                a gist (from-originals is structural — never summary-of-summary),
                or empty gist text (a gist that displays nothing is worse than
                no gist: the display fallback would be defeated).
        """
        if not member_ids:
            raise GistMembershipError(
                "a gist needs at least one member", context={"persona_id": persona_id}
            )
        offending = [m for m in member_ids if f"::{GIST_KIND}::" in m]
        if offending:
            raise GistMembershipError(
                "a gist may not be another gist's member (from-originals only)",
                context={"persona_id": persona_id, "member_ids": ",".join(offending)},
            )
        if not text.strip():
            raise GistMembershipError(
                "gist text must be non-empty", context={"persona_id": persona_id}
            )

        gist_id = make_gist_id(persona_id, member_ids)
        gist = PersonaChunk(
            id=gist_id,
            text=text,
            created_at=created_at,
            band=1,
            member_ids=tuple(member_ids),
            provenance=ChunkProvenance(
                source=WriteSource.SYSTEM,
                logical_id=gist_id,
                version=1,
                written_at=created_at,
                written_by=written_by,
            ),
        )
        # Only the GIST kind is ever addressed: this path structurally cannot
        # touch a raw chunk's row (text/embedding/lifecycle all untouched).
        self._backend.upsert(persona_id=persona_id, store_kind=GIST_KIND, chunks=[gist])
        self._emit(
            persona_id,
            AuditAction.WRITE,
            chunk_ids=[gist_id],
            written_by=written_by,
            metadata={"members": str(len(member_ids))},
        )
        return gist

    # ----- read / drill -----------------------------------------------------

    def gists(self, persona_id: str) -> list[PersonaChunk]:
        """Every gist row for the persona."""
        return self._backend.get_all(persona_id=persona_id, store_kind=GIST_KIND)

    def query(self, persona_id: str, query: str, top_k: int) -> list[PersonaChunk]:
        """Dense recall over the gist rows — K9's gist leg (the RAPTOR collapsed pool).

        Gists carry their own embeddings (K8-D-2), so they are searched as peers of the raw
        chunks in K9's fused candidate set (fuse-don't-route; K8 handover §3 — gist-as-key).
        Returned gists carry ``distance``.
        """
        return self._backend.query(
            persona_id=persona_id, store_kind=GIST_KIND, text=query, top_k=top_k
        )

    def covering_gists(self, persona_id: str, chunk_ids: Iterable[str]) -> dict[str, PersonaChunk]:
        """Map each listed raw-chunk id to the gist that covers it (if any).

        In-process over the persona's gist rows — gist counts are a small
        fraction of chunk counts (one per cluster window), so this stays cheap;
        a promoted reverse index is the documented push-down if it ever isn't.
        """
        wanted = set(chunk_ids)
        covering: dict[str, PersonaChunk] = {}
        for gist in self.gists(persona_id):
            for member in gist.member_ids:
                if member in wanted and member not in covering:
                    covering[member] = gist
        return covering

    def drill(self, persona_id: str, gist_id: str) -> list[PersonaChunk]:
        """The drill-down chain: a gist's raw members, in gist order (§0).

        Bottoms out at the untouched 100% originals. Served through
        ``get_by_logical_ids`` — every episodic writer mints ``logical_id ==
        chunk id`` (fresh chains, D-05-12 lineage), so the members' chains ARE
        the members; the fetch is indexed on both transports. A member that no
        longer exists (privacy-deleted) is simply absent — the cascade
        (:meth:`remove_gists_for_members`) removes such gists, but a reader
        racing the cascade still gets the surviving originals, never an error.
        """
        gist = next((g for g in self.gists(persona_id) if g.id == gist_id), None)
        if gist is None or not gist.member_ids:
            return []
        members = self._backend.get_by_logical_ids(
            persona_id=persona_id,
            store_kind="episodic",
            logical_ids=list(gist.member_ids),
        )
        by_id = {c.id: c for c in members}
        return [by_id[m] for m in gist.member_ids if m in by_id]

    # ----- the privacy cascade (K8-D-14) -------------------------------------

    def remove_gists_for_members(self, persona_id: str, deleted_ids: Iterable[str]) -> int:
        """Delete every gist whose membership intersects ``deleted_ids``.

        The K8-D-14 cascade: a derived artifact must not outlive its evidence —
        the gist TEXT may carry the deleted content, so the whole gist goes;
        the next engine pass regenerates from the surviving originals. Returns
        the number of gists removed (the honest report; zero is a valid
        answer, never a silent skip). Audited when non-zero.
        """
        gone = set(deleted_ids)
        doomed = [g.id for g in self.gists(persona_id) if gone & set(g.member_ids)]
        if not doomed:
            return 0
        self._backend.delete_documents(persona_id=persona_id, store_kind=GIST_KIND, ids=doomed)
        self._emit(
            persona_id,
            AuditAction.DELETE,
            chunk_ids=doomed,
            written_by="episodic.privacy_cascade",
            metadata={"cascaded_from": str(len(gone))},
        )
        return len(doomed)

    def delete_all(self, persona_id: str) -> None:
        """Drop every gist for the persona (the store-wide delete's cascade)."""
        existing = self.gists(persona_id)
        self._backend.delete_persona(persona_id, GIST_KIND)
        if existing:
            self._emit(
                persona_id,
                AuditAction.DELETE,
                chunk_ids=[g.id for g in existing],
                written_by="episodic.privacy_cascade",
            )

    # ----- audit -------------------------------------------------------------

    def _emit(
        self,
        persona_id: str,
        action: AuditAction,
        *,
        chunk_ids: list[str],
        written_by: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        from datetime import UTC, datetime

        self._audit.emit(
            AuditEvent(
                timestamp=datetime.now(UTC),
                persona_id=persona_id,
                action=action,
                store=self._STORE,
                source=WriteSource.SYSTEM,
                written_by=written_by,
                chunk_ids=chunk_ids,
                logical_ids=chunk_ids,
                metadata=metadata or {},
            )
        )


def resolve_display(
    chunks: Sequence[PersonaChunk],
    covering: dict[str, PersonaChunk],
) -> list[PersonaChunk]:
    """Band-resolved display (K8-D-11): pure — no I/O, fully testable.

    FULL chunks (band 0) and gist rows pass through untouched. A demoted chunk
    (band 1) renders as its covering gist, prefixed with the exact
    :data:`OLDER_MEMORY_MARKER`; several demoted hits under one gist dedupe to
    a single rendered entry (same memory, one summary). A demoted chunk with
    NO covering gist stays raw (the K8-D-3 fail-soft: never display nothing).
    The retrieval SET is unchanged — which memories were found is decided
    before this function; only display fidelity changes. Rendered entries are
    fresh PersonaChunks (hash computed over the rendered text) — the stored
    rows are never mutated.
    """
    out: list[PersonaChunk] = []
    rendered_gists: set[str] = set()
    for chunk in chunks:
        if chunk.band != 1 or chunk.member_ids:
            out.append(chunk)
            continue
        gist = covering.get(chunk.id)
        if gist is None:
            out.append(chunk)  # fail-soft: no gist yet ⇒ raw display
            continue
        if gist.id in rendered_gists:
            continue  # already rendered for a sibling hit (dedupe)
        rendered_gists.add(gist.id)
        out.append(
            PersonaChunk(
                id=gist.id,
                text=f"{OLDER_MEMORY_MARKER} {gist.text}",
                created_at=chunk.created_at,
                band=1,
                member_ids=gist.member_ids,
                distance=chunk.distance,
            )
        )
    return out
