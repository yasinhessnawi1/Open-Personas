# Changelog, persona-core

All notable changes to `persona-core` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-spec entries are added by the close-out phase of each spec. The authoritative
project-wide changelog lives at [`/CHANGELOG.md`](../../CHANGELOG.md); this file
mirrors only the `persona-core`-touching surface.

---

## [Unreleased]

(empty, future post-v0.1 work lands here)

---

## [1.0.0]: 2026-06-07

> **First public, API-stable release of the open-source `persona-core` library under Apache 2.0 (D-11-8).** Includes the Spec V1 JWT-verifier extraction, the Spec 19 amendment set landed during the v0.1 close-out (file-write produced-files / host-out debug logging / credits-service domain relocation), and all prior spec close-outs (Spec 01 schema/stores → Spec 18 routing). Per-package version pin tracks the library's stable public API; product v0.1.0 = the git tag on the system release.

### Added (Spec V1 cross-spec extraction)

- **`persona.auth.jwt_verifier`**: `make_jwt_verifier(config)` + `AuthenticatedUser` factory; new `JwtVerifierConfig: Protocol` (structural subtype satisfied implicitly by `APIConfig` and `VoiceConfig` via the `jwt_algorithms_list` `@property`). `AuthenticationError` relocated from `persona_api.errors` to `persona.errors`; `python-jose[cryptography]` moved to persona-core deps. persona-api re-exports preserve byte-for-byte back-compat (D-V1-X-jwt-verifier-extraction; Spec 08 additive amendment, 9th in the additive-extension chain per D-12-X / D-16-X / D-F4-X-bare-ref-resolution precedent).
  - Source: [`packages/core/src/persona/auth/jwt_verifier.py`](src/persona/auth/jwt_verifier.py).

### Added (Spec 19 amendment set, chain entries 14 / 15 / 20)

- **L2 (chain 14) D-19-X-file-write-produced-files**: `file_write` tool surfaces produced-files metadata via the produced-files contract so downstream renderers consume host-written outputs identically to sandbox-produced files. Closed-spec additive extension; no Spec 03 reopen.
- **L4 (chain 15) D-19-X-host-out-debug-logging**: host-out path emits one debug log entry per produced-file emission (loguru `persona.tools.file_write` component). Diagnostic-only; gated behind log level.
- **L6c (chain 20) D-19-X-credits-service-domain-relocation**: credits-service domain primitives relocated from `persona-api` into `persona-core` (domain home; API keeps the persistence + composition root). Boundary types stay Pydantic v2 frozen `extra="forbid"` per D-12-14 precedent; persona-api `services.credits_service` re-exports the domain shapes to preserve all existing call sites.

### Inherited (close-out roll-up from 2026-05-29, 2026-06-06)

The following persona-core close-outs are folded into the 1.0.0 anchor; full per-spec rationale lives in [`/CHANGELOG.md`](../../CHANGELOG.md):

- **Spec 17, Data Analysis and Visualisation.** `data_analysis` built-in skill pack; bytes-persistence layer (`CodeSandbox.copy_produced_file_to` + `read_produced_file_bytes`; `ProducedFileSizeError`); runtime call site for cross-turn `intermediate/*` staging.
- **Spec 16, Document Generation Skills.** Four built-in skill packs (`docx_generation`, `pptx_generation`, `xlsx_generation`, `pdf_generation`); M1a runtime affordance (`persona.skills.collect_skill_supplements`); D-16-2-supplements-relative-path production fix.
- **Spec 15, Image Generation.** `ImageBackend` Protocol + OpenAI gpt-image-1 + Flux 1.1 [pro] (fal.ai) backends; `identity.visual_style` additive schema extension; three-layer safety with categorical hard-line filter; `make_generate_image_tool` AsyncTool factory; `merge_visual_style` deterministic suffix-conditioning template.
- **Spec 14, Document Ingestion.** `DocumentChunk` sibling schema; `DocumentStore` conversation-scoped store; five parsers (txt/md/code, csv, docx, xlsx, pdf) with lazy-import + `MissingDependencyError` discipline; size-aware ingest strategy with D-14-1 3000-token threshold + ladder.
- **Spec 13, Vision and Multimodal Input.** `ImageContent` + `ImageRef` schema; `images` JSONB column shape; Pillow downscale-with-hard-ceiling.
- **Spec 12, Code Execution Sandbox.** `CodeSandbox` Protocol; `LocalDockerSandbox` (R-12-2 hardening); `make_code_execution_tool`.

### Inherited (prior versions, Spec 11 launch and earlier)

The 1.0.0 anchor subsumes the prior per-spec `[0.x.0]` line: Spec 11 launch (SSRF guard, structured `tool_calls`, streaming `call_id` reconstruction), Spec 04 skills, Spec 03 tools/MCP/Toolbox, Spec 02 backends, Spec 01 foundation (schema, stores, registry, history manager, audit). See [`/CHANGELOG.md`](../../CHANGELOG.md) `[1.0.0]` (2026-05-29) and prior `[0.x.0]` blocks.
