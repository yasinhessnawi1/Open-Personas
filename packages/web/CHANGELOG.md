# Changelog, persona-web

All notable changes to `persona-web` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-spec entries are added by the close-out phase of each spec. The authoritative
project-wide changelog lives at [`/CHANGELOG.md`](../../CHANGELOG.md); this file
mirrors only the `persona-web`-touching surface.

---

## [Unreleased]

### Added, Memory, the interactive knowledge-graph UI (Spec K5)

- **`/memory` area**: a 2-D-canvas force-graph (graphology ForceAtlas2 in a Web Worker):
  pan/zoom/neighbourhood-highlight, node colour by the seven `NodeKind`s, typed-edge encoding
  (causal red+arrow · temporal green-dashed · entity gold-solid · semantic faint-dotted),
  degree-sized hubs, far-zoom Louvain LOD, collision-avoided degree-priority labels, zoom-to-fit.
- **Editorial detail panel**: kind icon-square, provenance-as-story with a persona avatar,
  evolution timeline, traversable typed-link rows (colour swatches), the K4 care mark,
  content-only correction (`PATCH`, title hidden), consequence delete (`DELETE`),
  "Open conversation". Search-to-fly (K1) with a "⏎ fly to" hint.
- **Windowed working set**: seed → focus-expansion (merge) → eviction cap (`window-ops`,
  unit-tested); never draws the whole graph.
- Nav is **availability-gated**; a distinct "Memory isn't available here" state where no graph
  store exists (vs the "no memories yet" invite). New deps: `graphology`, `-layout-forceatlas2`,
  `-communities-louvain`. Dev harness at `scratch/memory` (NODE_ENV-guarded).
### Notification Coverage Completion (Spec P6)

- **Deep-link foundation**: `useNotify()`'s `NotifyOptions`/`NotificationEntry` gain
  an optional `href`; bell rows with an `href` render as keyboard-reachable anchors
  that route, mark themselves read, and close the panel. Added `markRead(id)` for
  single-entry read. Additive to Spec 35 (hrefless callers unchanged).
- **Low-balance-at-load**: a headless `LowBalanceWatcher` reads `GET /v1/me/credits`
  once per session; below the server-computed threshold (`low_balance && balance > 0`)
  it emits a persistent warning deep-linking to billing, at most once per session
  (`sessionStorage`-guarded). Best-effort; never disrupts load.
- **Durable, cross-device bell feed**: a `ServerNotificationsProvider` (mounted once)
  polls `GET /v1/me/notifications` on load / window focus / a light interval and
  surfaces the server-authored feed (run-terminal, persona-ready). The bell renders
  the **union** of this durable feed and the client-session low-balance advisory,
  newest-first, with deep-links. A newly-seen notification toasts once, suppressed
  for a run-terminal whose run is the current route (no double-signal); the bell
  entry lands regardless.
- **i18n**: `notifications.{run.*,persona.ready,personaFallback,lowBalance.*}` keys;
  server rows carry a locale-neutral `message_key` + `params`, resolved at render.

---

## [0.15.0]: 2026-06-07

> Subsumes Spec F4 (rich-output UI surface) + Spec F3 (file-input UI surface) + the Spec 19 amendment chain entry 18 (low-balance warning UI). Capability-UI specs built entirely from F2 primitives consuming Spec 12 / 13 / 14 / 15 / 16 / 17 / 06 / 08 contracts. **599 vitest tests across 54 files** (F3 baseline 400 → +199 from F4 work).

### Added (Spec 19 amendment, chain entry 18)

- **L6a (chain 18) D-19-X-low-balance-warning-ui**: credits-balance read surfaces a low-balance UI cue when the API's `CreditsResponse.low_balance` field is true (under-10 000 threshold per D-11-12). Banner placement matches F2 platform shell conventions; routes blocked at 402 surface the cue + retry-after copy. Closed-spec additive extension; no Spec 09 / Spec 11 reopen.

### Added (Spec F4, Rich-Output UI Surface, Phase 5 complete; Phase 6 pending operator-pass + sign-off)

- **`OutputContent` discriminated union + Zod schema** at [`src/lib/api/output-content.ts`](src/lib/api/output-content.ts): six variants (`inline-image` / `inline-chart` / `download-doc` / `result-block` / `working` / `failure`) with `kind` discriminator; `.strict()` per variant mirrors Pydantic `extra="forbid"`. D-F4-X-renderer-normaliser-shape.
- **chat + run normalisers via shared `_classify.ts`** at [`src/lib/normalisers/`](src/lib/normalisers/): `chatSseToOutputContent(event)` and `runEventToOutputContent(event)` produce IDENTICAL OutputContent for the same produced_file payload. Transport-shape leakage stops here (D-09-1).
- **`RunStep.outputs` view-time derivation** in [`src/lib/run.ts`](src/lib/run.ts): `runViewFromEvents` accumulates per-step `outputs: OutputContent[]` from tool_calling + tool_result events; no backend `Step` schema change (D-F4-X-output-derivation-shape).
- **F4 renderer set** at [`src/components/chat/output/`](src/components/chat/output/): `<InlineVisual>` (R-F4-4 one-component with intent prop) + `<DownloadChip>` (Bearer-auth blob download) + `<ResultBlock>` (monospace + truncation + collapsible Shiki code via React.lazy + Suspense) + `<WorkingState>` (F1 ToolRunningIndicator visual reused verbatim) + `<OutputDispatcher>` + `<OutputList>` (six-variant exhaustive switch + path-traversal defence-in-depth) + `<ImageLightbox>`.
- **MessageElement + StepCard surface integration**: `message-element.tsx` InterleavedContent emits dispatcher per recognized capability tool alongside ToolCallCard; `step-card.tsx` consumes derived `step.outputs` via `<OutputList>`. SAME renderer set across both surfaces.
- **`<AuthedImage>` F2 promotion**: strangler-fig move to `src/components/ui/authed-image.tsx`; re-export shim at the F3 path preserves all existing imports (D-F4-X-authedimage-f2-promotion).
- **Structural invariants test surface** at [`src/components/chat/output/__tests__/structural-invariants.test.tsx`](src/components/chat/output/__tests__/structural-invariants.test.tsx): six cross-cutting assertions: dispatcher exhaustiveness, 1MB-stays-by-reference (F3 T22 mirror), single-renderer-set parity across transports, path-traversal swap-to-failure, cross-surface DOM identity, dispatch-table parity with R-F4-1. 35 tests.
- **Playwright scaffold** at [`e2e/f4-rich-output.spec.ts`](e2e/f4-rich-output.spec.ts): 8 journeys (7 acceptance criteria coverage + 1 structural invariant journey); CSA-3 operator-passed at sign-off.

### Added (Spec F3, File-Input UI Surface, Phase 5 complete; Phase 6 pending operator-pass + sign-off)

- **Composer file-attach surface**: single attach control with content-type dispatch (D-F3-1); image preview tray; conversation-scoped document panel (D-F3-2 chip-only); inline image render in sent message bubbles via Bearer-authed blob URLs; at-send `NoVisionErrorBanner` safety net; deployment-honest no-vision tooltip on the disabled attach button.
- **Composer state slice + upload orchestration** at [`src/components/chat/composer/use-composer-attachments.ts`](src/components/chat/composer/use-composer-attachments.ts): image-attached state + per-image upload progress + per-image retry/remove. Conversation-switch state reset via sole-dep `useEffect(() => setAttachedImages([]), [conversationId])`.
- **Shared multipart upload service** at [`src/lib/upload.ts`](src/lib/upload.ts): `uploadImage(personaId, file, ...)` + `uploadDocument(personaId, conversationId, file, ...)`. XMLHttpRequest for byte-level upload progress; AbortController with two-phase early-exit; Bearer token + structured ApiError mapping.
- **`useAuthedImageBlobUrl` hook** at [`src/lib/hooks/use-authed-image-blob-url.ts`](src/lib/hooks/use-authed-image-blob-url.ts): fetch with Bearer auth, blob → `URL.createObjectURL`. Full 4-behaviour discipline asserted in tests.
- **`useObjectURL` hook** at [`src/lib/hooks/use-object-url.ts`](src/lib/hooks/use-object-url.ts): composer-local preview URL lifecycle with full 3-transition cleanup discipline per D-F3-X-preview-cleanup-discipline.
- **`useChat.send` strangler-fig extension** at [`src/lib/hooks/use-chat.ts`](src/lib/hooks/use-chat.ts): accepts optional `attachedImages: ImageRef[]`; threads them into `PostMessageRequest.images`. SSE consumption + `RunEvent` envelope + error-toast routing + reconnect behaviour ALL UNCHANGED.
- **F3-local composer components** at [`src/components/chat/composer/`](src/components/chat/composer/): `<ComposerAttachControl>`, `<ComposerImagePreview>`, `<DocumentChip>`, `<ConversationDocumentList>`, `<NoVisionErrorBanner>`.
- **`gen-api.sh` surgical fix** at [`scripts/gen-api.sh`](scripts/gen-api.sh): `PYTHONPATH` workaround for the Spec 01 D-01-9 hidden-`_editable_impl_` `.pth` skip on uv 0.6.x + CPython 3.13.
- **`src/lib/api/limits.ts`**: API-sourced caps with `// API source: <file>:<line>` comments on every constant.
- **Structural defence tests** at [`src/lib/hooks/use-chat-body-size.test.ts`](src/lib/hooks/use-chat-body-size.test.ts): three STRUCTURAL regression tests enforce Concern #4 store-by-reference invariant: 4×1MB-ref message body < 2 KB; text-only body < 500 B; body size linear in reference count.

### Inherited (prior versions)

The 0.15.0 anchor subsumes the persona-web Spec F2 close-out (`[persona-web 0.13.0]` 2026-06-06 in the project-wide CHANGELOG): component system + platform shell, the retokenised UI primitive kit (T03-T12), persona-identity components (T13-T16), measured-locked streaming-text renderer (T17), platform shell + layout primitives (T19-T20), interaction patterns (T21-T23), theme + i18n sweep (T24-T25), six rebuilt screens (T26-T31), and the component reference + Storybook defer artifact + criterion-#11 evidence package. Plus Spec 10 authoring draft + refine UI seam and Spec 09 web-app foundation (auth, chat keystone, run viewer, authoring marquee, settings, landing, i18n).
