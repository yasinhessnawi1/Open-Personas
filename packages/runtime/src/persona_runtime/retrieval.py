"""Shared per-turn conditioning retrieval (extracted from the text loop, D-V5-6).

The persona-conditioning retrieval — reading identity from its store and
querying self-facts / worldview / episodic for *this* turn — is the single
authoritative representation of "what the persona knows right now." Both the
text :class:`~persona_runtime.loop.ConversationLoop` and the voice turn
(spec V5) MUST share it: reimplementing it would risk conditioning drift
between modalities, which is the persona-bypass the voice spec forbids
(spec V5 §8; criteria 1+2).

This module is the extraction point. :func:`retrieve_context` was previously
``ConversationLoop._retrieve``; the loop now delegates to it byte-identically
(the only behavioural change is *where* the code lives). The added ``identity``
keyword is the spec-V5 D-V5-1 session-cache hook: identity is immutable at
runtime (Spec 01), so a voice session reads it once and passes it back each
turn, skipping the redundant ``get_all`` — while the variable stores are still
queried per turn. The text loop never passes it, so its behaviour is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_runtime.prompt import GraphContext, RetrievedContext

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from persona.schema.chunks import PersonaChunk
    from persona.stores.protocol import MemoryStore

    from persona_runtime.unified_recall import UnifiedProjection

#: The four typed stores, in the fixed order they are consulted each turn — the
#: order ``retrieve_context`` reports them through the ``on_recall`` hook (Spec
#: 35 D-35-4, the chat "thinking / remembering" staged state).
_RECALL_ORDER = ("identity", "self_facts", "worldview", "episodic")

__all__ = [
    "DEFAULT_RETRIEVE_TOP_K",
    "EARLY_RETRIEVE_TOP_K",
    "dynamic_top_k",
    "reinforce_recalled",
    "retrieve_context",
]

#: Floor top-k for the per-turn variable-store queries (the text loop's
#: historical ``_RETRIEVE_TOP_K``). Reached once the live history is long
#: enough to carry the recent context itself.
DEFAULT_RETRIEVE_TOP_K = 3

#: Upper bound used at the very start of a conversation, before the live
#: history carries any recent context. A fresh turn has spare prompt budget
#: and needs more retrieved memory to establish continuity from prior
#: sessions, so it pulls this many chunks and decays toward the floor.
EARLY_RETRIEVE_TOP_K = 8

#: Minimum number of *recency*-ordered episodic chunks always folded in
#: (alongside similarity) once a conversation is in progress, so the tail of
#: the previous session surfaces even when the current message is not
#: semantically near it ("what were we talking about?").
_MIN_RECENCY = 2


def dynamic_top_k(
    history_turns: int,
    *,
    high: int = EARLY_RETRIEVE_TOP_K,
    low: int = DEFAULT_RETRIEVE_TOP_K,
) -> int:
    """Per-turn retrieval budget that shrinks as the live history grows.

    A fresh conversation (``history_turns <= 0``) has no recent context in the
    prompt yet and ample budget, so it pulls ``high`` chunks to establish
    continuity from prior sessions. Each subsequent turn the live history
    already carries the recent context and the prompt fills, so the budget
    decays one chunk per accumulated turn down to the ``low`` floor.

    Args:
        history_turns: Turns already in the managed history (the
            conversation-progression signal). ``0`` is the first turn.
        high: Budget at the start of a conversation.
        low: Floor budget once history carries the recent context.

    Returns:
        The retrieval budget for this turn, in ``[low, high]``.
    """
    if history_turns <= 0:
        return high
    return max(low, high - history_turns)


def _recall_episodic(
    store: MemoryStore,
    persona_id: str,
    user_message: str,
    k: int,
    *,
    recency: bool,
) -> tuple[list[PersonaChunk], tuple[str, ...]]:
    """Episodic recall for one turn: similarity, optionally recency-augmented.

    With ``recency=False`` this is the historical behaviour — the top-``k``
    chunks by semantic similarity to ``user_message``. With ``recency=True``
    the most-recent turns are guaranteed a place in the budget (deduped against
    the similarity hits) so cross-session continuity does not depend on the
    current message embedding near a past one. The merged result is returned
    oldest-first so the prompt reads chronologically.
    """
    similar = store.query(persona_id, user_message, k)
    if not recency:
        found = similar
    else:
        n_recent = min(k, max(_MIN_RECENCY, k // 2))
        recent = store.recent(persona_id, n_recent)
        seen = {c.id for c in recent}
        extra = [c for c in similar if c.id not in seen][: max(0, k - len(recent))]
        found = [*recent, *extra]
        found.sort(key=lambda c: c.created_at)
    # The FOUND raw ids are recorded before display resolution (reinforcement
    # targets them — a demoted hit must reinforce to re-promote, K8-D-3/5);
    # the returned list is the DISPLAYED view (K8-D-11).
    found_ids = tuple(c.id for c in found if not c.member_ids)
    return _resolve_display(store, persona_id, found), found_ids


def _resolve_display(
    store: MemoryStore, persona_id: str, chunks: list[PersonaChunk]
) -> list[PersonaChunk]:
    """Band-resolved episodic display (Spec K8, K8-D-11) — duck-typed, fail-soft.

    A demoted (band-1) hit renders as its covering gist with the exact
    ``[older memory — summarized]`` marker; which memories were FOUND is
    decided above (embeddings are permanent), only display fidelity changes.
    Duck-typed like the reinforce hook (the D-05-4 discipline): a store/double
    without ``resolve_display`` — or a resolution failure — falls back to raw
    display, never to a broken turn.
    """
    resolve = getattr(store, "resolve_display", None)
    if resolve is None:
        return chunks
    try:
        return list(resolve(persona_id, chunks))
    except Exception:  # noqa: BLE001 — deliberately fail-soft (display only)
        from persona.logging import get_logger

        get_logger("runtime.retrieval").warning(
            "band display resolution failed persona={p}; raw display", p=persona_id
        )
        return chunks


def reinforce_recalled(
    stores: Mapping[str, MemoryStore],
    persona_id: str,
    context: RetrievedContext,
) -> None:
    """Reinforce this turn's recalled episodic chunks (Spec K8, K8-D-5).

    The explicit post-retrieval command that keeps :func:`retrieve_context`
    pure (CQS): ``strength += 1`` + decay-clock reset for every RAW episodic
    chunk that surfaced this turn (gist rows carry ``member_ids`` and are
    excluded — reinforcing members through a gist hit is K9's recall-path
    call). One batched store command per turn; empty recall is a no-op.

    Execution context is the CALLER's obligation (gate ruling): the chat loop
    calls this synchronously on the request's owner-scoped engine; the voice
    path calls it inside its retrieval worker thread, which already runs off
    the event loop (``asyncio.to_thread`` — the starvation rule holds
    structurally). Fail-soft by construction: reinforcement is an optimisation
    of future recall, so a store without the command (duck-typed, the D-05-4
    discipline) or a failed write must never break the turn.
    """
    ids = (
        list(context.episodic_recalled_ids)
        if context.episodic_recalled_ids
        else [c.id for c in context.episodic if not c.member_ids]
    )
    if not ids:
        return
    reinforce = getattr(stores.get("episodic"), "reinforce", None)
    if reinforce is None:
        return  # a store/double without the K8 command — reinforcement is optional
    try:
        reinforce(persona_id, ids)
    except Exception:  # noqa: BLE001 — deliberately fail-soft (never break a turn)
        from persona.logging import get_logger

        get_logger("runtime.retrieval").warning(
            "episodic reinforcement failed persona={p} ids={n}", p=persona_id, n=len(ids)
        )


def retrieve_context(
    stores: Mapping[str, MemoryStore],
    persona_id: str,
    user_message: str,
    *,
    top_k: int = DEFAULT_RETRIEVE_TOP_K,
    identity: list[PersonaChunk] | None = None,
    history_turns: int | None = None,
    on_recall: Callable[[str, int], None] | None = None,
    graph_retrieval: Callable[[str], GraphContext] | None = None,
    core_block_provider: Callable[[], str | None] | None = None,
    unified_recall: Callable[[str], UnifiedProjection] | None = None,
) -> RetrievedContext:
    """Retrieve this turn's conditioning context from the typed stores.

    Identity comes from ``identity.get_all(persona_id)`` (session-constant);
    self-facts / worldview / episodic from ``query(persona_id, user_message,
    top_k)`` (per turn). This is the shared conditioning-retrieval used by both
    the text loop and the voice turn (D-V5-6) — never reimplemented.

    Args:
        stores: The four typed memory stores keyed by kind (``identity`` /
            ``self_facts`` / ``worldview`` / ``episodic``).
        persona_id: The persona whose stores to read.
        user_message: This turn's message — the query for the variable stores.
        top_k: How many chunks to retrieve per variable store.
        identity: Pre-fetched identity chunks (the D-V5-1 voice session cache).
            ``None`` (the default, and the text loop's path) reads identity from
            its store this call — byte-identical to the historical behaviour.
        history_turns: Turns already in the managed history. When given, the
            per-turn budget is computed dynamically (:func:`dynamic_top_k` —
            high on a fresh turn, decaying to ``top_k`` as history grows) and
            episodic recall is recency-augmented so the previous session's tail
            surfaces regardless of semantic match. ``None`` (the default)
            preserves the historical fixed-``top_k`` similarity-only behaviour.
        on_recall: Spec 35 (D-35-4/D-35-5) optional hook called once per store,
            in ``_RECALL_ORDER``, as ``on_recall(store, count)`` where ``count``
            is the number of chunks that store contributed this turn. The text
            loop passes a collector that maps each call to a
            ``RunEvent.memory_recall`` SSE frame (the chat "thinking /
            remembering" state). ``None`` (the default, and the voice turn's
            path — D-35-5) emits nothing: retrieval stays silent, behaviour
            unchanged. The hook is invoked *after* all stores are read, so it
            never reorders or perturbs retrieval itself. The graph is NOT
            reported here — its surfacing is K5's UI concern, and ``on_recall``
            stays the four persona stores so the existing contract is unchanged.
        graph_retrieval: The K3 enrichment (D-K3-X-a2-seam) — an owner-scoped
            ``query -> GraphContext`` callable (built by
            :func:`~persona_runtime.graph_selection.make_graph_retrieval`),
            queried with this turn's message. ``None`` (the default) ⇒ an empty
            graph, so the text loop and A2's legs are byte-identical until graph
            retrieval is composed in. The graph is an **independent** source: it
            does not touch, reorder, or gate the persona-store retrieval above.
        core_block_provider: The K9 core-memory seam (K9-D-10) — a ``() -> str | None``
            callable that reads the persona's current always-in-context block (built by
            :func:`~persona.recall.core_memory.read_core_block`). ``None`` (the default) ⇒
            no block, byte-identical. The block is background-refreshed elsewhere; this
            only READS it for injection — no build/summarise on the turn path (acceptance-7).
        unified_recall: The K9 unified recall (K9-D-1/D-11) — a ``query -> UnifiedProjection``
            callable (built by :func:`~persona_runtime.unified_recall.make_unified_recall`)
            that fuses the pyramid AND the graph into one reranked+gated path and projects it
            back into ``episodic`` + ``graph``. When given, it REPLACES the separate episodic
            recall and graph retrieval (identity/self-facts/worldview unchanged); ``None`` (the
            default) ⇒ today's two-path behaviour, byte-identical (K9-D-X-backcompat).

    Returns:
        The :class:`RetrievedContext` the prompt builder conditions on.
    """
    resolved_identity = identity if identity is not None else stores["identity"].get_all(persona_id)
    dynamic = history_turns is not None
    k = dynamic_top_k(history_turns) if history_turns is not None else top_k

    # K9 (T8/T9): when the unified recall is composed, ONE fused+reranked+gated path over the
    # pyramid AND the graph replaces the separate episodic recall and graph retrieval — the
    # fuse-don't-route evolution (K9-D-1/D-11), projected back into these same fields. Identity
    # / self-facts / worldview are unchanged (not part of the fuse). ``None`` ⇒ today's two-path
    # behaviour, byte-identical (K9-D-X-backcompat).
    if unified_recall is not None:
        projection = unified_recall(user_message)
        episodic_displayed = projection.episodic
        episodic_found_ids = projection.episodic_recalled_ids
        graph_context = projection.graph
    else:
        episodic_displayed, episodic_found_ids = _recall_episodic(
            stores["episodic"], persona_id, user_message, k, recency=dynamic
        )
        graph_context = (
            graph_retrieval(user_message) if graph_retrieval is not None else GraphContext()
        )

    context = RetrievedContext(
        identity=resolved_identity,
        self_facts=stores["self_facts"].query(persona_id, user_message, k),
        worldview=stores["worldview"].query(persona_id, user_message, k),
        episodic=episodic_displayed,
        episodic_recalled_ids=episodic_found_ids,
        graph=graph_context,
        core_block=core_block_provider() if core_block_provider is not None else None,
    )
    if on_recall is not None:
        counts = {
            "identity": len(context.identity),
            "self_facts": len(context.self_facts),
            "worldview": len(context.worldview),
            "episodic": len(context.episodic),
        }
        for store in _RECALL_ORDER:
            on_recall(store, counts[store])
    return context
