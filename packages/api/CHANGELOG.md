# Changelog — persona-api

All notable changes to `persona-api` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-spec entries are added by the close-out phase of each spec. The authoritative
project-wide changelog lives at [`/CHANGELOG.md`](../../CHANGELOG.md); this file
mirrors only the `persona-api`-touching surface.

---

## [Unreleased]

### Added — Memory read/edit endpoints (Spec K5)

- **`GET /v1/memory/graph|nodes/{id}|search`, `PATCH`/`DELETE /v1/memory/nodes/{id}`** — the
  knowledge-graph UI's backend: windowed, RLS-scoped via the per-request engine, pure projection
  of K0 types (no graph logic in the route). `graph` is the seed window or a focus neighbourhood;
  `search` wraps K1 hybrid retrieval; `PATCH` routes to `correct_node`, `DELETE` to `delete_node`.
- **`MemoryWindowResponse.available`** — distinguishes *no usable graph store* (community-on-
  SQLite / graph off) from *an empty graph*, so the UI shows a distinct unavailable state instead
  of the "no memories yet" invite. `enable_graph_writes` gates on the engine dialect (the K0 graph
  is Postgres-only), so `graph_store` is `None` on non-Postgres and the routes degrade gracefully.
- **`MemoryProvenanceView.persona_name`** (nullable) — for the panel's provenance avatar;
  `persona_id → name` resolution in `node_detail` is a recorded follow-up (R-K5-PROV-PERSONA).
- Seed-index migration `ix_graph_nodes_owner_created` (renumbers off main's head at merge-back,
  R-K5-MIG-RENUMBER).
### Notification Coverage Completion (Spec P6 — durable cross-device feed)

- **`notifications` table + migration** (owner-scoped, FORCE RLS `owner_id =
  current_setting('app.current_user_id')`, read/unread, locale-neutral
  `message_key` + `params` JSONB). Idempotency key `UNIQUE (owner_id, kind, ref_id)`
  makes a server-authored write once-only across the run persist-final /
  persist-error paths + the two avatar paths. Migration number is a placeholder
  chained off the local head; **recomputed off main's real head at merge-back**.
- **`GET /v1/me/notifications`** (RLS-scoped, newest-first, paginated) + mark-read
  (`POST …/read-all`, `POST …/{id}/read`). Adversarial cross-tenant RLS test
  (non-superuser role, non-vacuous both directions, WITH CHECK, fail-closed).
- **Server-authored writes** — run-terminal at `RunRegistry._persist_final` /
  `_persist_error`; persona-ready via a shared `persona_service.write_persona_ready`
  called by both the in-process `set_avatar_url` and the durable-queue avatar
  handler. Both **best-effort** (never fail the authoritative run/avatar persist)
  and idempotent. Copy stored locale-neutral; the web localises.
- **OpenAPI client regen** for the two `me` endpoints is deferred to merge-back.

### Synthetic-Media Provenance & Disclosure (Spec R3 — EU AI Act Art. 50)

- **`personas.avatar_source` column + migration `025_avatar_source_provenance`** (nullable
  `TEXT`: `generated` / `uploaded` / `NULL` = unknown). Split-home + idempotent
  `ADD COLUMN IF NOT EXISTS` (mirrors migration 008); no RLS change, no `schema_version` bump.
  Backfill = NULL = unknown (no audit-backfill — old uploads/generations are not reliably
  distinguishable after the fact).
- **Provenance co-written with `avatar_url` across all four write paths** (R3-D-3): the inline
  create-hook (`set_avatar_url` → `generated`), the async `AvatarGenerationHandler` compare-and-set
  (`generated`), the upload-to-change PATCH (`update_persona` → `uploaded`), and the
  create-with-user-avatar INSERT (`create_persona` → `uploaded`). Same write ⇒ no NULL window;
  RLS-scoped ⇒ no cross-tenant leak.
- **Derived Art. 50 disclosure** (`services.provenance.ai_generated_from_source`, the single
  derivation): `PersonaDetail` gains `avatar_source` + derived `avatar_ai_generated`;
  `ArtifactMetadataView` gains derived `ai_generated`. `PersistedArtifact` (persona-core) gains
  `ai_generated` (defaults `True`) so the SSE inline-render payload (chat + voice) discloses a
  generated image — it previously carried none. Response-schema additions → the web TS client
  regenerates at merge-back; the visible "AI-generated" badge is a web-render follow-up.

---

## [0.16.0] — 2026-06-07

> Subsumes Spec 17 (data analysis), Spec 12 Phase 5+6 close-out (code-execution sandbox), Spec 14 (document ingestion), Spec 16 (document generation skills), Spec 15 (image generation), Spec 13 (vision and multimodal input), and the Spec 19 amendment set landed during the v0.1 close-out (Spec 14 integration test L7 + memory_chunks.kind CHECK migration L9 + credits-service domain relocation L6c API side).

### Added (Spec 19 amendment set — chain entries 20 / 21 / 23)

- **L6c (chain 20) D-19-X-credits-service-domain-relocation (API side)** — `persona_api.services.credits_service` re-exports the relocated domain shapes from `persona.credits` (persona-core); persistence + composition root stay in persona-api. All existing call sites (Spec 08 deduct-after-success, Spec 10 author/refine flat-credit deductions, Spec 12 code-execution per-execution flat deduct, Spec 15 pre-deduct + refund) untouched.
- **L7 (chain 21) D-19-X-spec14-integration-test** — new integration test surface at [`packages/api/tests/integration/`](tests/integration/) covers the Spec 14 document-upload + parse + chunk + store + cross-tenant 404 path against live Postgres + pgvector; closes the §9 #14 deferral from Spec 14 Phase 5.
- **L9 (chain 23) D-19-X-memory-chunks-kind-check-migration** — Alembic migration adds a `CHECK (kind IN ('identity','self_facts','worldview','episodic','document'))` constraint on `memory_chunks.kind`; structural defence against arbitrary-string kind injection bypassing the four-store typology + DocumentStore sibling.

### Added (Spec 17 — Data Analysis and Visualisation, Phase 5 + 6 close-out)

- **V4-aligned chart serve surface** — bytes at `<workspace>/<owner_id>/<persona_id>/charts/<id>.png` served by the existing `GET /v1/personas/:id/uploads/charts/<id>.png` route via `image_service.fetch` slash-aware ref logic. Zero route changes, zero service changes.
- **Hosted bytes-persistence implementation** at [`src/persona_api/sandbox/hosted.py`](src/persona_api/sandbox/hosted.py) — E2B `sandbox.files.read` + `target_path.write_bytes`. **`runtime_tool.py:216-244` D-F4-X-bare-ref-resolution three-branch persister policy fix**: charts/ + intermediate/ stay at workspace root (load-bearing); everything else routes into `uploads/<filename>.<ext>` so the slash-aware resolver lands on the right path. 9 regression tests.
- Runtime call site composition at [`src/persona_api/sandbox/runtime_tool.py`](src/persona_api/sandbox/runtime_tool.py) + [`src/persona_api/services/runtime_factory.py`](src/persona_api/services/runtime_factory.py) — `produced_file_persister` injected; outer `make_pool_code_execution_tool` builds persister closure + augments input-files provider for `intermediate/*` cross-turn staging.

### Added (Spec 12 — Code Execution Sandbox, Phase 5 + 6 close-out)

- **`HostedSandbox`** at [`src/persona_api/sandbox/hosted.py`](src/persona_api/sandbox/hosted.py) — wraps E2B Code Interpreter SDK (`e2b-code-interpreter>=1.0,<2`, lazy-imported); substrate per D-12-12.
- **`SandboxPool`** at [`src/persona_api/sandbox/pool.py`](src/persona_api/sandbox/pool.py) — multi-tenant lifecycle composer; per-user cap `max_per_user=2` (D-12-17); pool-owned `asyncio.Task` reaper at 60s cadence; idempotent acquire on `(owner_id, conversation_id)`. `SandboxQuotaExceededError → 429` + `SandboxUnavailableError → 503` handlers in [`src/persona_api/errors.py`](src/persona_api/errors.py).
- **`code_execution` toolbox wiring** at `RuntimeFactory._build_toolbox` — adds the tool when `sandbox_pool` is configured; `SandboxRequestContext` contextvar threading via `chat_service.stream_chat`; D-12-3 flat per-execution credits deduction (`credits_service.deduct(reason="code_execution")`).
- **T12 multi-perspective adversarial security pass** — STRUCTURAL-CLEAR fixes for F-T12-RES-02 (`wall_clock_s` via `asyncio.wait_for` + force-kill on timeout), F-T12-RES-01 (SCP-12-4 ceiling docs + warning log), F-T12-INT-01 (`:` rejection at both `SandboxRequestContext.__post_init__` and `SandboxPool._make_session_id`).

### Added (Spec 14 — Document Ingestion, Phase 5)

- **`document_service.upload`** at [`src/persona_api/services/document_service.py`](src/persona_api/services/document_service.py) — workspace+sidecar layout under `resolve_sandbox_path`; `DocumentRef` API-boundary type; `remove_all_for_conversation` cascade-helper.
- **`routes/uploads.py` content-type dispatch** at [`src/persona_api/routes/uploads.py`](src/persona_api/routes/uploads.py) — CSA-2 dispatcher: `image/*` → `image_service.upload` (Spec 13); document MIME types → `document_service.upload`. Unknown formats → 415.
- **`routes/documents.py`** at [`src/persona_api/routes/documents.py`](src/persona_api/routes/documents.py) — `GET /v1/conversations/:id/documents` (list) + `DELETE /v1/conversations/:id/documents/:ref`. RLS-scoped via `chat_service.get_conversation` (404 if cross-tenant).
- **Conversation cascade-delete extension** at [`src/persona_api/routes/conversations.py`](src/persona_api/routes/conversations.py) — `DELETE /v1/conversations/:id` now cascade-cleans document workspace files + DocumentStore chunks via `document_service.remove_all_for_conversation`.
- **Scanned-PDF vision handoff** in `document_service.upload` — rasterise via `pypdfium2` (BSD/Apache-2.0 per D-14-X-pdf-library-license) at 150 DPI, persist as PNGs under workspace, return `DocumentRef.images` with Spec 13 `ImageContent` references.

### Added (Spec 16 — Document Generation Skills, Phase 5 + 6 close-out)

- **API composition wiring** at [`src/persona_api/sandbox/runtime_tool.py`](src/persona_api/sandbox/runtime_tool.py) + [`src/persona_api/services/runtime_factory.py`](src/persona_api/services/runtime_factory.py) — `deferred_input_files_provider` injected; M1a supplements staged into `/workspace/in/.skills/<name>/supplements/<topic>.md`.

### Added (Spec 15 — Image Generation)

- **`credits_service.refund`** at [`src/persona_api/services/credits_service.py`](src/persona_api/services/credits_service.py) — reverse-deduct ledger entry per D-15-X-credit-flow-semantics pattern (a).
- **Per-user advisory-lock cap=1** at [`src/persona_api/imagegen/concurrency.py`](src/persona_api/imagegen/concurrency.py) — `pg_try_advisory_xact_lock(('x' || md5(:user_id))::bit(64)::bigint)` inside `rls_engine.begin()`. Multi-worker-correct from day one.
- **`persona_api.imagegen.service.generate`** at [`src/persona_api/imagegen/service.py`](src/persona_api/imagegen/service.py) — composition root: cap acquisition + provider call live INSIDE one `rls_engine.begin()` block. Pre-deduct credits BEFORE backend call per D-15-X-pre-deduct-credits. Bytes persisted at D-13-4 layout `{workspace_root}/{owner_id}/{persona_id}/uploads/<blake2b>.<ext>`.
- **`POST /v1/personas/:id/imagegen` route** at [`src/persona_api/routes/imagegen.py`](src/persona_api/routes/imagegen.py) + startup wiring in [`src/persona_api/app.py`](src/persona_api/app.py) — auth + pre-flight RLS persona check (404 cross-tenant) + credits pre-flight gate (402) + service-layer dispatch + API-layer audit. Two-audit-emission discipline.

### Added (Spec 13 — Vision and Multimodal Input)

- **`PersonaDetail.capabilities` additive field** at [`src/persona_api/schemas/responses.py`](src/persona_api/schemas/responses.py) — `{vision: bool, configured_tiers: tuple[str, ...]}`. Hydrated via the public `TierRegistry.supports_vision_for(name)` + `configured_tier_names` contract (D-F3-X-tier-registry-public-contract).
- **Pillow added as a `packages/api` dependency**; license-stack discipline per D-13-X-pillow (HPND vs Apache-2.0 — `persona-core` stays Apache-2.0-only).

### Inherited (prior versions)

The 0.16.0 anchor subsumes Spec 11 launch (credits zero-guard 402, two Grafana dashboards, soak-test harness, `LAUNCH_CHECKLIST.md`, Deploy artifacts), Spec 10 authoring (draft+refine endpoints + parser + corpus eval harness), Spec 09 web pairing (auto-title, `personas.avatar_url`, `DELETE /v1/conversations/:id`, CORS, JIT user provisioning, chat SSE `tool_calling`/`tool_result` events), Spec 08 hosted-service foundation (`create_app`, RLS engine via pool listener, auth+JWT verification, all routes/services/middleware). Full per-spec rationale lives in [`/CHANGELOG.md`](../../CHANGELOG.md).
