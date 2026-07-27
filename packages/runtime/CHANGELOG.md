# Changelog, persona-runtime

All notable changes to `persona-runtime` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-spec entries are added by the close-out phase of each spec. The authoritative
project-wide changelog lives at [`/CHANGELOG.md`](../../CHANGELOG.md); this file
mirrors only the `persona-runtime`-touching surface.

---

## [Unreleased]

(empty, future post-v0.1 work lands here)

---

## [0.18.0]: 2026-06-07

> Spec 18, Unified Model Router close-out + Spec 19 amendment chain entry 13 (prompt-builder produced-files verification). Strangler-fig discipline preserves Spec 05's `Router` byte-for-byte: existing `test_router.py` 25/25 + `test_router_vision.py` 10/10 pass unchanged. The agentic-loop routing seam is sharpened to honour the unified router's two-layer architecture without disturbing Spec 06's plan-act-reflect cycle.

### Added (Spec 18, Unified Model Router)

- **`Router` + `RouterScorer` Protocols** at [`src/persona_runtime/routing/protocol.py`](src/persona_runtime/routing/protocol.py): `@runtime_checkable`. `Router.route(context: RoutingContext) -> RoutingDecision`. `RouterScorer` is the v0.2 extras seam for the optional learned-router integration (D-18-1).
- **`HeuristicRouter`** at [`src/persona_runtime/routing/heuristic.py`](src/persona_runtime/routing/heuristic.py): Spec 05's rule-based router refactored behind the Protocol. `.choose()` preserved verbatim (byte-for-byte regression guarded). Strangler-fig alias at [`src/persona_runtime/router.py`](src/persona_runtime/router.py) re-exports `HeuristicRouter as Router` per D-18-X-strangler-fig-alias-shape.
- **`UnifiedRouter`** at [`src/persona_runtime/routing/unified.py`](src/persona_runtime/routing/unified.py): Layer 1 hard-filter via `apply_constraint_filter` + Layer 2 sweet-spot scorer + bounded fallback (voice 30ms / text 100ms per D-18-4). Four fallback reasons: `"timeout"` / `"scoring_error"` / `"empty_metadata"` / `"partial_metadata:<tier>"` with rate-limited loguru warning per (reason, profile) per 60s (D-18-X-fallback-instrumentation).
- **`apply_constraint_filter` free function** at [`src/persona_runtime/routing/layer1.py`](src/persona_runtime/routing/layer1.py): shared by `HeuristicRouter.route()` AND `UnifiedRouter.route()` via module-level import (D-18-X-layer1-extraction).
- **`RoutingContext` + `RoutingDecision` boundary types** at [`src/persona_runtime/routing/types.py`](src/persona_runtime/routing/types.py): frozen Pydantic v2 + `extra="forbid"` (D-05-9 precedent).
- **`TierMetadata` + `TierRegistry.metadata_for()`** at [`src/persona_runtime/tier.py`](src/persona_runtime/tier.py): additive extension at the runtime layer (NOT on `ChatBackend` Protocol per Phase 1 fold-in d). 6 fields per D-18-3: cost_input/output_per_1k, first_token_latency_ms, throughput_tokens_per_sec, context_window, tool_strength. `tier_metadata_from_env(prefix)` ships the env-var population path.
- **`FirstTokenLatencyTracker`** at [`src/persona_runtime/routing/latency.py`](src/persona_runtime/routing/latency.py): per-model EWMA tracker (α=0.2) with simple-average warm-up for samples 1-5 (D-18-X-first-token-measurement-impl). Hooked into `ConversationLoop._stream_round` at the first non-empty `chunk.delta`.
- **TurnLog routing extension** at [`src/persona_runtime/logging.py`](src/persona_runtime/logging.py): additive D-18-X-turnlog-extension fields: `routing_decision: RoutingDecision | None`, `routing_latency_ms: float`, `routing_fallback_triggered: bool`, `routing_fallback_reason: str | None`. Pre-Spec-18 callers stay green (all optional with safe defaults). JSON round-trip verified for Postgres JSONB compatibility.

### Added (Spec 19 amendment, chain entry 13)

- **L1 (chain 13) D-19-X-prompt-builder-produced-files-verification**: `PromptBuilder` now verifies prior-turn produced-files references against the active workspace before rendering context; resolver mismatches raise the structural domain exception so downstream renderers never reach a stale path. Closed-spec additive extension; no Spec 05 reopen. Coordinates with the Spec F4 `_persist_produced_file` policy fix at the API layer (D-F4-X-bare-ref-resolution).

### Inherited (close-out roll-up from prior versions)

The 0.18.0 anchor subsumes the runtime-touching surface of intermediate Phase 2 work folded into prior `[Unreleased]` blocks:

- **Spec F4, Rich-Output UI Surface.** `RunEvent.tool_result` constructor at [`src/persona_runtime/agentic/events.py:96-103`](src/persona_runtime/agentic/events.py): 4-line additive edit forwards `result.data.produced_files` onto the event payload. One constructor serves BOTH chat SSE AND RunEvent transports. No Pydantic schema change. D-F4-X-event-kind-for-produced-files.

### Inherited (Spec 11 launch and earlier)

The 0.18.0 anchor subsumes Spec 11 launch (`_dispatch` recovery on ToolNotAllowedError/ToolExecutionError, assistant-with-tool_calls native-path emission, `StepHistoryCompactor._recent_start` boundary correction), Spec 10 authoring (chat-stream/SSE bridge tightening), Spec 09 web-app pairing (chat-stream delta-by-delta + `tool_calling`/`tool_result` SSE events + real `done.tier`), Spec 06 agentic loop (full `AgenticLoop` + `Run`/`Step`/`RunEvent` + `StepHistoryCompactor`), and Spec 05 conversation loop (`ConversationLoop` + `PromptBuilder` + `Router` + `TierRegistry` + `TurnLog`). Full per-spec rationale lives in [`/CHANGELOG.md`](../../CHANGELOG.md).
