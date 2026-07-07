"""Unified-recall tunables, read from ``PERSONA_RECALL_*`` env vars (Spec K9, T1).

The knobs the K9 recall path reads — all additive, all env-tunable (the
``GraphSettings`` / ``EpisodicSettings`` precedent; no magic numbers). This module
carries the **fusion + rerank + budget** knobs the T1/T2 foundation needs; the composite
score weights (T4), the abstention thresholds (T6), the contiguity budget (T4/T6), the
diffusion weight (T5), and the core-block budget (T7) are added by their own tasks so
each lands with the code that reads it (scope discipline).

Defaults are the Phase-3 starting points; the reranker top-k split (chat vs voice) rests
on the measured CPU micro-benchmark (K9-D-3 — top ~8–12 fits the voice slack off-loop,
top-20 is the chat budget); P8 calibrates the fusion weights/quotas against real recall.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["RecallSettings"]


class RecallSettings(BaseSettings):
    """K9 recall tunables (``PERSONA_RECALL_*``).

    Attributes:
        rrf_k: The RRF constant (Cormack 2009; K1's default 60). Higher flattens the
            rank contribution; the fusion is rank-based, so this is the only fusion
            hyper-parameter besides the per-leg weights/quotas (K9-D-2).
        weight_episodic_raw: RRF weight on the raw-episodic leg.
        weight_episodic_gist: RRF weight on the gist leg.
        weight_graph_dense: RRF weight on the graph dense leg.
        weight_graph_sparse: RRF weight on the graph sparse (BM25/FTS) leg.
        weight_graph_traversal: RRF weight on the graph one-hop leg (kept ≤ the dense
            weight by default — traversal augments, never displaces, mirroring K1-D-3).
        quota_episodic_raw: Max candidates the raw-episodic leg contributes to the pool.
        quota_episodic_gist: Max candidates the gist leg contributes to the pool.
        quota_graph_dense: Max candidates the graph dense leg contributes.
        quota_graph_sparse: Max candidates the graph sparse leg contributes.
        quota_graph_traversal: Max candidates the graph one-hop leg contributes.
            Per-source quotas stop one dominant leg crowding the graph/gists out of the
            fused pool (K9-D-2) — recall balance, not ordering (the reranker orders).
        pool_size: The fused-pool size handed to the reranker (the top-k it re-scores).
        result_budget: The final number of candidates returned to prompt assembly.
        rerank_top_k_chat: Fused top-k the chat (synchronous, GPU) reranker re-scores
            (K9-D-3: ~20 — generous budget).
        rerank_top_k_voice: Fused top-k the voice (off-loop, tiny-encoder) reranker
            re-scores (K9-D-3: ~8–12 — the measured slack; top-20 p95 is the risk edge).
        rerank_enabled: The reranker kill-switch (K9-D-3/D-4). Default ``False`` — until P7
            lands, the path runs the fused order (byte-equivalent to the stub). The
            composition consults it to decide whether to build a real reranker; when off,
            :func:`~persona.recall.rerank.build_reranker` returns the fused-order path.
        rerank_timeout_ms_chat: Wall-clock deadline for the synchronous (chat) rerank; a
            breach degrades to the fused order (provisional — the real chat budget is the
            P7-coordinated V100 p50/p95 measurement, K9-D-3 condition 1).
        rerank_timeout_ms_voice: Deadline the voice path applies off the event loop (T9);
            120 ms sits ~2.3× above the measured top-12 p95 (51 ms) so a normal rerank
            never trips it, but a stall degrades to fused within the spoken-turn slack.
        rerank_model: HuggingFace hub id of the cross-encoder behind the reranker seam
            (:mod:`persona.recall.scorer`). Default is the K9-measured MiniLM-L6-class
            reference (top-12 p95 ~51 ms / top-20 p95 ~111 ms on CPU); P7 swaps a
            stronger chat-tier model here behind the same ``Scorer`` Protocol.
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_RECALL_", extra="ignore")

    # --- rollout gate (T8/T9) -----------------------------------------------
    # The unified recall replaces the two separate paths (episodic recall + graph
    # retrieval) with one fused+reranked+gated path (K9-D-1/D-11). Default OFF: the
    # composition wires it but the loop runs today's two-path recall byte-identically
    # until this is flipped in-env (validated by the operator pass — K9-D-X-backcompat).
    unified_enabled: bool = Field(default=False)

    # --- fusion (T2, K9-D-2) ------------------------------------------------
    rrf_k: int = Field(default=60, gt=0)
    weight_episodic_raw: float = Field(default=1.0, ge=0.0)
    weight_episodic_gist: float = Field(default=1.0, ge=0.0)
    weight_graph_dense: float = Field(default=1.0, ge=0.0)
    weight_graph_sparse: float = Field(default=1.0, ge=0.0)
    weight_graph_traversal: float = Field(default=0.5, ge=0.0)
    quota_episodic_raw: int = Field(default=20, gt=0)
    quota_episodic_gist: int = Field(default=20, gt=0)
    quota_graph_dense: int = Field(default=20, gt=0)
    quota_graph_sparse: int = Field(default=20, gt=0)
    quota_graph_traversal: int = Field(default=10, gt=0)

    # --- pool / budget / rerank top-k (T2/T3, K9-D-3) -----------------------
    pool_size: int = Field(default=20, gt=0)
    result_budget: int = Field(default=10, gt=0)
    rerank_top_k_chat: int = Field(default=20, gt=0)
    rerank_top_k_voice: int = Field(default=12, gt=0)
    rerank_enabled: bool = Field(default=False)
    rerank_timeout_ms_chat: int = Field(default=800, gt=0)
    rerank_timeout_ms_voice: int = Field(default=120, gt=0)
    rerank_model: str = Field(default="cross-encoder/ms-marco-MiniLM-L6-v2", min_length=1)

    # --- composite score (T4, K9-D-5, refined) ------------------------------
    # Additive over ABSOLUTE [0,1] components (relevance = reranked score, recency,
    # importance) — NOT min-max-normalised. Relevance is PRIMARY (1.0) with recency
    # and importance as smaller tie-breakers (0.35): the 1/1/1-with-min-max prior
    # violated K9-D-5's own "recency only breaks ties; old high-relevance still
    # surfaces" invariant (min-max destroys magnitude, letting recency override a
    # large relevance gap). Vetoes live in the abstention gate, never the score.
    # P8 calibrates; any weighting (incl. 1/1/1) stays config.
    weight_relevance: float = Field(default=1.0, ge=0.0)
    weight_recency: float = Field(default=0.35, ge=0.0)
    weight_importance: float = Field(default=0.35, ge=0.0)
    # Graph nodes carry no strength counter, so their recency is a plain exp decay
    # over the most-recent provenance time (episodic uses the strength-aware
    # ``retention()`` from EpisodicSettings). Hours; larger = slower decay.
    recency_tau_hours: float = Field(default=168.0, gt=0)

    # --- temporal contiguity (T4, K9-D-6/D-7 — the DrillStop contract) -------
    # k_c ≤ k_s (EM-LLM's measured partition): a reserved MINORITY budget, seeded
    # from the top hit(s) only. β is the similarity-dropoff stop (guard 2): a
    # neighbour with a relevance reading below β·seed stops the walk.
    contiguity_budget: int = Field(default=4, ge=0)
    contiguity_seed_count: int = Field(default=2, ge=0)
    contiguity_beta: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- graph diffusion tiebreaker (T5, K9-D-8) ----------------------------
    # A low-weight (~0.1) additive tiebreak, gated to detected multi-hop/bridge
    # queries only (GAAMA's w_ppr=0.1 precedent). The rule stack: low one-hop
    # confidence OR ≥ min_entities distinct query entities, plus seed dispersion.
    diffusion_weight: float = Field(default=0.1, ge=0.0)
    diffusion_min_query_entities: int = Field(default=2, ge=1)
    diffusion_confidence_floor: float = Field(default=0.35, ge=0.0, le=1.0)
    diffusion_max_seeds: int = Field(default=3, ge=1)
    diffusion_per_seed_neighbours: int = Field(default=5, ge=1)

    # --- abstention gate (T6, K9-D-9) ---------------------------------------
    # Deterministic: abstain unless top_rerank ≥ floor AND (margin ≥ margin_floor
    # OR mass ≥ mass_floor). Thresholds are the Phase-3 starting points; the eval
    # slice sweeps them to a coverage target (never a hardcoded guess in prod).
    abstain_floor: float = Field(default=0.30, ge=0.0, le=1.0)
    abstain_margin_floor: float = Field(default=0.05, ge=0.0, le=1.0)
    abstain_mass_floor: float = Field(default=0.60, ge=0.0)
    abstain_mass_count: int = Field(default=3, ge=1)

    # --- core-memory block (T7, K9-D-10) ------------------------------------
    # The always-in-context user+persona summary. Refreshed in the BACKGROUND on
    # K8's engine cadence (never the turn path); the turn only reads + injects it.
    # ``core_enabled`` gates whether the composition reads/injects it (default ON,
    # but absent ⇒ byte-identical regardless — K9-D-11). ``core_token_budget`` caps
    # the summary; ``core_max_sources`` bounds how many raw chunks feed a refresh.
    core_enabled: bool = Field(default=True)
    core_token_budget: int = Field(default=200, ge=8)
    core_max_sources: int = Field(default=40, ge=1)
