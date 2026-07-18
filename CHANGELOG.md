# Changelog

All notable changes to Open Persona are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-spec entries are added by the close-out phase of each spec.

---

## [Unreleased]

### Universal real-cost metering & credit billing (Spec M3, 2026-07-15)

> Most paid surfaces charged a flat fee (or nothing) regardless of what the
> provider actually cost us — silent margin loss and no cost visibility. M3 makes
> EVERY paid surface recover its real cost through one seam and one formula from
> one living pricing table: `credits = max(floor, ceil(MARKUP × (provider_cents +
> infra_flat_cents)))` at 1 credit = 1¢, `MARKUP` default 1.0 (pass-through). Each
> ledger row records the true pre-markup cost + how it was priced.

#### Added
- **All-surface usage ledger** — `GET /v1/me/usage/ledger` returns the
  `credit_transactions` log across every billed surface (chat, authoring, image
  + true-ups, agentic runs, task legs, background LLM, voice per-turn + LiveKit,
  avatar, sandbox), each row carrying its `cost_basis` provenance + credits
  moved. The existing `/v1/me/usage` (chat turn_logs) is unchanged.
- **Voice per-turn owner billing + mid-call cutoff** — a live call bills the
  caller its real cost per committed turn (served STT + TTS + LLM, priced at the
  *actually-served* provider's rate — Gladia/ElevenLabs primary, Deepgram/Cartesia
  fallback) plus a LiveKit infra tick at call end, off the audio loop + idempotent
  + fail-soft; on credit exhaustion the call ends (one grounded spoken notice, then
  the room is closed).
- **Idempotent billing** — `credit_transactions.billing_key` (partial-unique) so
  at-least-once background/task/voice deducts never double-charge on retry.
- **One pricing truth** — `persona.billing` (formula + `MeteredBilling` seam +
  pricing registry) in persona-core; a docs↔registry sync test keeps
  `docs/pricing/pricing-table.md` honest. New `cost_basis` provenance vocabulary
  (`actual_openrouter` / `estimate_static` / `estimate_catalog` / `provider_meter`
  / `infra_flat` / `unpriced`).

#### Changed
- **Chat, authoring, image, avatar, agentic runs, task legs, background LLM**
  (episodic consolidation / voice-autopick / initiative scan) now bill their REAL
  provider cost (OpenRouter `usage.cost` actuals where present, else a priced
  estimate), replacing flat fees. Image adds a ceiling pre-deduct trued-up to the
  real cost. Agentic runs meter per step and cut off on exhaustion.
- **Sandbox execution** reclassified into the M3 schema (`infra_flat` basis,
  `cost_cents` recorded); its flat ~1-credit charge is unchanged. Embeddings /
  connectors / MCP infra inside an enclosing op is subsumed by that op's floor —
  no separate hot-path charge.
- **`credit_transactions`** gains `cost_cents` (DOUBLE PRECISION, true pre-markup
  provider cost) + `cost_basis` + `billing_key` (migration 050, all additive).

### Utterance-level multilingual voice — code-switching STT + text-follow TTS (Spec V14, 2026-07-15)

> A persona's voice pipeline was language-PINNED per call: Deepgram silently
> dropped a mid-utterance second language (R9-025), and Cartesia's voice was
> statically scoped to one language regardless of what the reply text said.
> Strategy A (Gladia STT + ElevenLabs TTS) makes both directions utterance-level
> — behind the same provider Protocol seam, one env flip each way.

#### Added
- **Gladia streaming + batch STT** (`PERSONA_STT_PROVIDER=gladia`) — no
  per-call language pin; code-switches within a single utterance (the measured
  fix for R9-025's "Deepgram drops the second language"). Streaming rides the
  live call pipeline; the one-shot dictation upload (`/v1/stt`) dispatches to
  the BATCH API (`transcribe_oneshot_batch`), guided (never locked) by an
  optional per-request language hint.
- **ElevenLabs streaming TTS** (`PERSONA_TTS_PROVIDER=elevenlabs`) — ONE voice
  speaks whatever language the reply TEXT is in; no `language_code` is ever
  sent on the wire (text-language auto-follow). Per-utterance WebSocket
  lifecycle, rail-native `pcm_24000` audio, expressivity accepted-and-ignored.
- **Dialect-aware auto-pick** — the create-time voice picker (and the
  auto-remap below) prefer a voice whose ElevenLabs `verified_languages`
  accent/locale suits the persona's declared language (e.g. an Egyptian-accent
  voice for an Arabic persona), grounded in the real `/v1/voices` metadata.
- **Boot-time voice AUTO-REMAP** — on API startup, a non-blocking background
  pass re-picks every persona whose stored voice no longer matches the active
  TTS provider (a Cartesia → ElevenLabs switch), so existing personas keep a
  distinct, provider-correct voice instead of falling to a shared default.
  Cheap-skips entirely on the default `cartesia` provider (zero DB reads).
- **Fail-soft voice-identity resolution** — a provider-mismatched persona voice
  (and the `/v1/tts` proxy's forwarded `provider` field) now fall back to the
  active backend's default instead of 4xx-ing; flipping the provider back
  restores every original voice untouched (nothing is mutated).
- **Reply-language mirroring for voice** — under an utterance-level TTS
  provider, the voice reply-language directive switches from a hard per-call
  pin to "default to the persona's language, mirror the user's when they
  write/speak in another one" (text chat is unaffected — byte-identical).
- `PERSONA_STT_LANGUAGE_HINT` is now deprecated (warns once on set); superseded
  by per-request language plumbing and Gladia's own code-switch guidance.

#### Known-open (owner leg)
- The Gladia BATCH one-shot dictation path was proven on synthetic clips only;
  a real bilingual-speech fidelity check
  (`test_gladia_batch_oneshot_real_bilingual_speech_fidelity`) is an
  owner-run `@external` gate — see the Spec V14 close-out evidence for the
  exact pass/fail criterion and escalation options if it fails.

## [1.1.0] - 2026-07-11

### Per-persona model selection — pick the brain, see the price (Spec M1, 2026-07-10)

> Choose the LLM a persona runs on from the live OpenRouter catalog, price tags
> included, with today's tier system as the default and the safety net.

#### Added
- **`routing.preferred_model`** on the persona (additive-optional): a capable
  chosen model serves the persona's chat turns via a single OpenRouter
  passthrough backend (any catalog id, env-gated on `PERSONA_OPENROUTER_API_KEY`,
  fail-open); any runtime error falls down the real tier chain. Personas without
  a choice are byte-identical to before — proven fail-closed e2e at the
  composition root.
- **`GET /v1/models`** — curated ten-model shortlist (live-validated, announced-EOL
  filtered) + browse-all, with USD-per-1M input/output prices served from the
  Spec-22 catalog client; fail-open `stale:true` envelope; `asyncio.to_thread`
  keeps the event loop clear.
- **`PersonaModelPicker`** in the persona editor (price tags, browse-all,
  "Use tier default"), plus a sticky per-user default: the last-chosen model
  pre-selects on the next persona create, and creating on tier-default clears it
  (`users.preferred_model`, migration 045, profile GET/PATCH).

### Operator-fix wave — chat titles, turn healing, schedule integrity (R9-020…023, 2026-07-10/11)

#### Fixed
- **Chat titles are real again — and self-improving** (R9-020): titling moved off
  the small tier (the documented confabulation root) onto its own surface
  (default mid, `PERSONA_API_TITLE_TIER`); a durable `title_refresh` job
  re-titles the whole conversation as it crosses 4/10/24/50/100 messages (a bad
  generation keeps the existing title); every title write pushes a live sidebar
  update.
- **A mid-stream crash can no longer brick a conversation** (R9-022): orphaned
  `running` assistant messages self-heal at the next turn start (in-process
  registry is the liveness authority), the startup sweep warns instead of
  whispering, and the residual insert race maps to 409 — never a raw 500.
- **Fired one-time schedules cannot become tick zombies** (R9-023): every
  re-arm path routes through one guarded seam (409 `schedule_state_conflict`
  on re-arming a fired one-time — including the fired-recurring→one-time
  conversion that once carried fire counts across); the scheduler tick
  reconciles existing zombies terminally, before any enqueue.
- **`personas.updated_at` bumps on every row mutation** (R9-021) — recently-
  updated ordering works; all seven write sites healed by one column `onupdate`.
- Preferred-model fallbacks (capability gate, missing passthrough key) now log
  at WARNING, once per conversation loop — a chosen-but-unroutable model is
  never silent; seven web page loaders fetch genuinely in parallel; the
  recall-scorer live suite skips honestly on the one documented dev-box GEMM
  fault instead of training everyone to ignore red.

### Event triggers — the persona's third impulse: react to typed events (Spec A7)

> "When an email from my landlord arrives, summarise it." A7 adds reaction to
> typed *platform events* (not only A1's clocks and A5's scans) **without a new
> actor**: an event trigger's only two consequences are **fire a confirmed
> contract task's leg** (A4's machinery, the trigger replacing the clock in the
> A1→A2 bridge) or **enqueue an A5 initiative candidate** — every A7 outcome
> already passes an existing consent/safety gate. Feature-gated OFF; inert until
> `PERSONA_EVENT_TRIGGERS_ENABLED=true`.

#### Added
- **`persona.events`** (`persona-core`) — the closed six-member `EventKind`
  catalogue (`connector.message_received`, `task.leg_completed/failed`,
  `task.milestone`, `connector.linked/unlinked`) with frozen typed envelopes, the
  per-kind `TriggerFilter` union (case-insensitive `contains`, no regex — A7-D-2),
  the two-member `TriggerAction` union, `TriggerSpec`, and `EventTriggerSettings`
  (`PERSONA_EVENT_TRIGGERS_*`).
- **`EventFire`** — a distinct `ResumeTrigger` variant carrying the causal chain
  that rides the queued leg across the connector→worker boundary (the
  cross-process loop guard, A7-D-6).
- **The trigger registry** (`event_triggers` table, migration `039`, split-home
  RLS) + the **dispatcher** (`persona_api.events`): match at the event birth
  point, act through the shared `JobQueue` via **exactly two doors** — a
  structural test proves no third path. Loop prevention (causal-chain refusal +
  depth cap), storm safety (cooldown coalesce-to-one + R7 pre-check, drop-with-
  audit + a P6 bell), and the A6-D-8 autonomy-pause seam gate every fire.
- **Door b** — the `event_candidate` job turns an event into an A5 candidate
  through the unchanged pipeline; a deterministic wellbeing subject-exclusion at
  the handler seam keeps gated-category content from ever becoming an unprompted
  initiative subject (criterion 8, K4 composition).
- **Create-via-chat** — an NL event-trigger recognizer (cue-gated small-tier
  judge, conservative: never guesses a watched sender) drafts a `TriggerSpec` that
  flows through the **existing** A4 echo → confirm → `OriginationService` door
  (the only creation path); the echo renders the concrete `When: whenever …`.
- **The A6 provenance contract** (`persona_api.events.provenance`) — the frozen,
  importable audit-action + `EventFire`-identity vocabulary A6 renders "ran
  because: {human}" from, guarded by a shape test against drift.
- Live wiring: the connector inbound path + worker lifecycle emit through the real
  dispatcher; unlinking a platform disables its triggers with a user notice.
- `PERSONA_EVENT_TRIGGERS_ENABLED` (default `false`) + `COOLDOWN_SECONDS` (300),
  `MAX_CHAIN_DEPTH` (3), `PER_OWNER_MAX_FIRES_PER_HOUR` (60), `FIRE_COST_ESTIMATE`
  (1).

### Real-time delivery — a background delivery surfaces live, no reload (Spec A11)

> When a persona acts while you're away, you shouldn't have to reload to find out.
> A11 adds the persistent user-level push channel so a background delivery appears
> live — the message in the open chat, the bell instantly — closing R4-C1-23 (the
> reload-to-see bug). It fills a reserved seam rather than inventing new machinery:
> the fan-out is the in-process bus both editions already run on.

#### Added
- **`persona_api.realtime`** — the out-of-turn SSE channel: a closed, versioned,
  data-only event catalogue (`notification.created` / `message.delivered` /
  `task.updated`) + `ready`/`resync` control events; the `epoch:seq` transport
  envelope (the `Last-Event-ID` cursor); the per-user event log + the restart-safe
  reconnect resolver (in-ring replay vs `resync{epoch_changed|ring_gap}` → the
  client full-refetches, no silent gap); the in-process `UserEventChannel` bus
  (per-tab **bounded** queue, `put_nowait` overflow→close, per-user RLS-scoped
  fan-out) + the `stream_user_events` generator.
- **`GET /v1/me/events`** — the persistent, RLS-scoped live channel (me-scope from
  the verified token, never a param; 15s heartbeat under Fly's ~60s SSE reap;
  `Last-Event-ID` resume; fail-soft 503 when unwired).
- **Web:** `MeEventsProvider` (one SSE connection, subscribe-once via `useMeEvent`,
  dedupe-by-id, resume, reconnect, fail-soft to P6's poll) — the bell goes live
  (`notification.created` → refetch `/v1/me/notifications`, unioned with P6), the
  open chat appends a background `message.delivered` (refetch + reconcile-by-id, no
  double-render vs the in-turn stream), the conversation list re-orders.

#### Changed
- Background originated deliveries now fan out through the channel-backed
  `LiveSessionRegistry` (the reserved `web_deliverer` seam) instead of
  `_NoLiveSessions` — a background `message.delivered` reaches an open tab live,
  emitted AFTER the recorder's durable commit.
- `run_terminal` (`RunRegistry`) and `schedule_executor_missing`
  (`ScheduledTaskFireHandler`) now ping the bell live (`notification.created`),
  published AFTER the durable notification commits (the client refetches).

#### Notes
- **No migration, no new dependencies.** The resume ring is in-memory (a dropped
  ring is a refetch, not data loss — durability is P6 + conversation reads); the
  bus is `asyncio.Queue` + an in-memory registry; the client reuses `consumeSSE`.
- **Edition-honest (A11-D-1/5):** the in-process bus is the whole fan-out mechanism
  for both editions as deployed (community SQLite in-process; cloud single Fly
  Machine). LISTEN/NOTIFY was spiked on Neon (works on the DIRECT endpoint ~38ms,
  silently dropped on the pooled endpoint) and documented as the drop-in forward
  path for when cloud goes multi-Machine — not built (nothing is split yet).

### Voice task origination — one grammar, both channels, via delegation (Spec A9)

> "remind me every morning" spoken in a call now works like typed in chat — but
> **voice never executes with the mid model**. A recognized task/schedule/autonomy
> ask is echoed + confirmed for the ear, then DELEGATED verbatim to the chat
> pipeline (frontier tier, full tools/K4/approvals/R7 — one audited execution
> path); voice says "I'm preparing that in the background" and speaks the grounded
> result back when it's done. Recognition sits off the realtime path behind a cheap
> cue regex — a no-intent turn's latency is unchanged (measured p95 0.07 ms). Ships
> **OFF** (`PERSONA_VOICE_DELEGATION_ENABLED=false`).

#### Added
- **`persona_voice.model.origination_gate`** — the voice origination gate: A4's
  grammar reused VERBATIM (recognizer + `is_affirmative_confirmation` + amendment
  interpreter) behind a VOICE echo mode; pure decision logic (no I/O / store /
  graph). The confabulation guarantee is structural — the gate creates nothing; a
  clean spoken confirm only returns the verbatim ask to delegate. Confirmation
  window (next-turn-only, barge-invalidated, timeout — A9-D-3); one amendment round
  then finish-in-chat (A9-D-4). Steering (pause/resume/cancel/reschedule) rides the
  same crossing (A9-T7).
- **`render_echo` VOICE mode** (`persona-runtime`, A9-D-2) — an additive
  `EchoMode{CHAT,VOICE}` on the A4 echo (mirroring V11's `PromptMode`); CHAT
  byte-identical (snapshot-pinned), VOICE speaks the same clauses for the ear
  (short sentences, no lists/markdown, grants as one clause, a single confirm ask).
- **`persona.jobs.delegation`** — the core-canonical `DelegatedTurnPayload`
  {`conversation_id`, `verbatim_ask`, `provenance`, `persona_id`, `dedup_token`} +
  `delegated_turn_idempotency_key` (draft-hash-primary over the verbatim ask +
  conversation; the gate-minted token escape hatch) — the one contract both writers
  share (A9-D-5).
- **`persona_voice.session.delegation_enqueue`** — the voice-side twin raw-INSERT
  writer (the `enqueue_voice_synthesis` discipline: column-parity + real-worker
  transition tested); **`delegation_dispatch`** — enqueue off-loop, track the
  hand-back on success, fail-soft (spoken "couldn't set it up", untracked) on an
  enqueue failure (A9-T10); **`delegation_handback`** — the voice-side poll of the
  `delegated_turn` job's terminal row → the grounded spoken hand-back at an idle
  floor (succeeded → what was actually done; blocked-on-approval → the honest
  "needs your OK in chat"; failed → fail-soft) (A9-D-5/D-7, T6).
- **`persona_api.jobs.handlers.delegated_turn`** — the api-side A0 handler: runs
  the verbatim ask through `RuntimeFactory.build_conversation_loop` on the frontier
  (the one audited path), creates a recognized standing intent via the unchanged
  `OriginationService` (voice's confirm stands in for the chat "yes" — loop.py
  untouched, no fake user turn), applies a delegated pause/resume via the unchanged
  `TaskSteeringService`, records the durable outcome (a final assistant message +
  `provenance=voice` + a `delegated_turn.execute` audit row, actor `voice_delegated`).
  Registered in `worker_root` as the task-leg tenant's sibling (built-but-inert
  until voice enqueues).
- **`PERSONA_VOICE_DELEGATION_ENABLED`** (default `false`) — the kill-switch; OFF
  ⇒ a byte-identical voice turn (the gate is never composed).

#### Changed
- The voice reply producer (`VoiceModelReplyProducer`) runs the gate strictly
  AFTER the R1-hard safety bypass (crisis precedence preserved — the gate is never
  consulted on a crisis turn) and BEFORE routing/retrieval; graph stays OFF; a gate
  failure degrades to today's clean ordinary turn (fail-soft). The A10 confabulation
  guard (`detect_schedule_claim`, reused verbatim) now also runs on the voice
  reply's non-delegated ordinary path — a hallucinated "I scheduled it" is corrected
  (spoken tail + folded into episodic), so the graph never learns a false create
  (A9-D-6).

### Per-tenant MCP — bring-your-own credentialed image servers, isolated (Spec N6)

> The deferred half of N1: a user's chosen IMAGE-runtime MCP server (a Docker image
> with no remote URL — e.g. `mcp/google-flights`, ~77% of the catalog) now runs
> **scoped to that user**, with **their** secret injected, **isolated** — resolving the
> D-N1-5 blocker (a shared gateway couldn't vary a per-user secret). Closes R4-C1-21:
> a persona assigned `mcp:google-flights` used to silently get no tools; now the image
> runs per-tenant and its tools reach the model, and an assigned-but-not-connected
> server shows a friendly badge instead of failing silently.

#### Added
- **`persona_api.mcp.runtime`** — the runtime-agnostic vocabulary: `MCPRuntimeState`,
  `NotConnectedReason`, `MCPServerConnection` (the `connected`/`not_connected(reason)`
  signal), `MCPRuntimeInstance`, and the `PerTenantMCPRuntime` Protocol.
- **`persona_api.mcp.fly` + `.fly_runtime`** — the ratified substrate (N6-D-1): a Fly
  Machine per (tenant, server) via the Machines REST API (`httpx`, no Fly SDK). `ensure`
  reconciles-by-deterministic-name and **adopts** an orphaned Machine across the
  spawn crash-window (zero double-spawn); a background reaper stops idle Machines
  cross-tenant.
- **`persona_api.mcp.secret_resolver`** — the live `GatewaySecretResolver` (the N1 seam,
  implemented at the api layer): a tenant's Fernet secret (reusing Spec-30's store +
  key, no second key) is injected as the Machine's **spawn env** — never a prompt, tool
  spec, tool result, log, or error body.
- **`persona_api.mcp.run_policy`** — the runnable-image allow-list (N6-D-4): Docker-
  official `mcp/` namespace + provenance + operator allow-list (`PERSONA_MCP_RUN_VETTED`,
  empty = deny-all), checked at BOTH assign and spawn (TOCTOU fail-closed). NOT
  signature-based.
- **Migration `mcp_runtime_instances`** — RLS-scoped per-tenant instance records
  (idempotency key `(owner_id, server_id)`; the reaper reads them under the bypass engine,
  ensure/signal under `persona_app`). *(Placeholder revision; renumbered at merge-back.)*
- **`GET /v1/personas/{id}/mcp-connections`** — the not-connected signal (R4-C1-21): each
  assigned server's `connected`/`not_connected(reason)`, RLS-scoped, secretless. The web
  renders it as a friendly badge (never a raw enum).
- Image-app adoption (`adopt_catalog_app` image branch): vetting re-check + the per-tenant
  cap (the (N+1)th enable denied with `MCPRuntimeCapacityError`, enforced at assign).
- Env: `PERSONA_MCP_RUNTIME_FLY_APP` / `FLY_API_TOKEN` / `_FLY_PORT` / `_IDLE_TIMEOUT_S` /
  `_REAP_INTERVAL_S` / `_MAX_PER_TENANT`, `PERSONA_ALLOW_PER_TENANT_MCP`,
  `PERSONA_MCP_RUN_VETTED`.

#### Security
- **Edition-honest (N6-D-5):** community keeps N1's local gateway, byte-unchanged; cloud
  runs the per-tenant runtime only behind `PERSONA_EDITION=cloud` + the explicit
  `PERSONA_ALLOW_PER_TENANT_MCP=1` ack (startup refuses otherwise) — no arbitrary
  third-party-container execution without the operator asserting the vetted, per-active-
  tenant-cost posture.
- **Isolation is structural:** two tenants enabling one server name get two Firecracker
  Machines, each with only its own secret; the credential flows user → Fernet store →
  spawn env → process, never the model boundary (proven adversarially, incl. a hostile
  error body echoing the secret).

### Autonomy UI — "what did my personas do while I slept?" (Spec A6)

> The web surfaces for autonomy: the morning **Review**, the **Approvals** inbox,
> and **Tasks** list/detail — plus the thin RLS endpoints they render. One digest
> builder feeds both the surface and C0's morning message; the approval inbox and
> chat resolve the SAME durable proposal (idempotent dual resolution); controls
> ride the real A2/A3/A5/A8 doors. Calm by default, loud only where loudness is
> information (stuck states). Backend minimalism: thin endpoints over existing
> stores, one migration pair.

#### Added
- **Activity area** — one nav row landing on the **Review** (`/review`); `Tasks`
  (`/tasks`, `/tasks/{id}`) + `Approvals` (`/approvals`) as siblings via
  `ActivityTabs`; `/runs` demoted to the task-detail "Open run" drill (A6-D-1).
- **Morning Review** (`GET /v1/autonomy/review`) — the shared `MorningDigest`
  builder (`digest/builder.py`), ordered waiting→stuck→done→initiatives + an
  upcoming strip, per-section caps with an honest overflow; one builder, two
  renderings (web + C0's `render_digest_message`). Dateline-spine render, the
  five-test calm rubric.
- **Approvals inbox** (`routes/approvals.py`, W4) — pending proposals rendered
  *verbatim* (see-then-grant); approve/deny/modify through the ONE shared
  `ApprovalResolutionService` chat also uses — **idempotent dual resolution**,
  the chat-vs-inbox race resolves once on the durable record (A6-D-3).
- **Tasks** (`routes/tasks.py`) — the cross-persona list (B1) with `stuck_cause`
  at list level (one `DISTINCT ON` query); the detail (contract+grants, budget+
  ledger, checkpoints as progress+next-step — never raw transcripts, A6-D-4, the
  terminal report as its own projection); commands (B2): pause/resume/cancel/
  budget-extend — audited, idempotent, bounded.
- **Autonomy controls** (B4, `routes/autonomy.py`) — owner-wide pause + per-
  persona suspend + the initiative dial; the W7 kill-switch panel (owner pause at
  the area root). At merge-back the owner-pause predicate was injected into **every**
  origination path (A6-D-8 completeness): the A5 scan, the A7 dispatcher, the A10
  tick, and the task-leg runner (`is_runnable`) all consult it — so the W7 copy now
  honestly says a pause **stops all autonomy** (its honesty guard-test flipped to
  require the full-scope claim).
- **Live-refetch seam** (W8) — `useTaskSignal`, transport-agnostic: refetch-not-
  trust, advance-only dedup, targeted refetch. Wired live at merge-back: the worker
  emits a data-only `task.updated` on the A11 channel at the terminal + waiting-on-
  user transitions (`publish_task_updated`), and the client bridges the frame onto
  the W8 bus. `DigestItem.ref` → per-item deep-links.
- **Persistence** — migrations `043_owner_autonomy_pause` + `044_deferred_digest`
  (renumbered onto main's head at merge-back); the `DeferredDigestStore` closes the
  over-cap chatter drop-gap (A6-D-10, atomic consume-and-mark).

#### Notes
- `ran_because` provenance renders from the A7 `event_trigger.fired` audit `human`
  (A7-D-9), sourced per task by the B5 builder at merge-back — a done/stuck task
  that ran from an event shows "ran because: …". A6 adds no new env vars.

### Routing — deliberate surface→tier policy; the tuning surface retired (Spec P9)

> The router stops guessing: a turn's model tier is a function of its SURFACE,
> stated once — not of turn counts or keyword-matching. Closes the mid/small
> confabulation root the R4 operator pass surfaced, at the tier level.

#### Added
- **`routing/policy.py`** (`persona-runtime`) — the policy table
  (chat/authoring/agentic = frontier · voice = the latency tier · background =
  small · recognition = mid), `tier_for(surface, pin, override)` (precedence
  pin > env-override > table) and `PolicyRouter` behind the Spec-18 Router
  Protocol (Layer-1 vision constraints still filter first, fail-loud preserved).
- `PERSONA_API_RECOGNITION_TIER` (default `mid`) — the A4/A8 intent interpreters
  (standing-intent / amendment / steering / reschedule) now compose on the
  recognition tier, never small (the confabulation root; small also measured
  SLOWER than mid — 926 ms vs 57 ms median TTFT).
- `PERSONA_ROUTING_INTELLIGENT_ENABLED` (default `false`) — the Spec-23
  model-within-tier scorer + budget machinery is globally DORMANT; the
  per-persona `intelligent.enabled` flag is only consulted when this gate is on
  (a stored `true` was a web-form artifact, not a choice). Structural test
  proves the hot path never touches the Spec-18/23 scorers.

#### Changed
- **Chat generation is frontier, deterministically** — no turn-1-only frontier,
  no mid downgrade, no boilerplate/persona-critical keyword tier-flipping
  (classifiers retired from the routing path; still computed for TurnLog
  observability). A pinned `tier_for_generation` still wins (deliberate
  override, honored — just no longer surfaced).
- **Voice generation routes the latency tier deterministically** — voice turn 1
  no longer rides frontier (a live latency hazard: no configured frontier model
  fits the 800 ms voice budget — measured), and boilerplate turns no longer dip
  to small. Fixed en route: the voice reply producer built its routing context
  with the TEXT profile — the Spec-18 voice profile never reached the real seam.
- **Agentic run steps are frontier for every step** (user-read output;
  supersedes the D-06-6 tool-continuation→mid grading).
- Authoring recommenders (`/recommend-tools`, `/recommend-capabilities`) moved
  off hardcoded mid onto the authoring tier (frontier); the author/refine
  routes' silent mid fallback now falls back to the policy default instead.
- Background surfaces (titles, compaction summaries, text_summarize) resolve
  via `tier_for("background")` — explicit, not incidental.

#### Removed
- **The per-persona routing tuning UI** (`RoutingSection`, Spec 31) + its
  persona-draft plumbing + 32 i18n keys. The `RoutingConfig` schema fields stay
  (old personas load byte-identically; stored pins survive every editor write
  verbatim — proven); no migration.

#### Fixed
- Latent fail-soft gap: two sibling `TierNotConfiguredError` classes exist and
  the interpreter/text_summarize composition only caught one — an unconfigured
  tier crashed loop construction instead of failing soft. Both now caught.
### Scheduling — the user's own create door + the confabulation close (Spec A10)

> The missing verb on the one schedule mechanism: the USER creates a schedule
> directly — deterministic, model-free — and the persona can no longer claim a
> schedule it didn't create.

#### Added
- **`POST /v1/me/schedule`** — create a schedule + its backing task through the
  existing `ScheduleStore` CAS door and the A2 task path (picker-state in, the
  server maps the rule; never-firing cadences 422 fail-fast; audited
  `actor=user_via_ui`, originator=user). Idempotent on a required client-minted
  key: a double-click converges on one task+schedule, two deliberate submits
  stay distinct (no content-hash dedup — a form submit is intent).
- **`POST /v1/me/schedule/preview`** — the create's confirm echo from the SAME
  shared engine preview as the reschedule twin (full tz-framed clause, the
  engine's next fire, the quiet-hours warn/offer; no write).
- **"New reminder" on the calendar** (`/schedule`) — subject + required executor
  persona + the reused A8 picker (now with an additive "Once, at…" kind the
  reschedule dialog inherits) → engine preview → confirm; the created occurrence
  renders immediately from the same occurrences read. Quiet-hours offer is
  actionable (one tap re-times to the nearest edge and re-previews) and never
  blocks.
- **The honesty gate** (`persona_runtime/schedule_claim.py`) — grounding-first:
  every legitimate schedule voice is emission-coupled, and ordinary generation
  structurally cannot create — so a free-text "I've scheduled it" there is false
  by construction. A precision-first trilingual (EN/NO/AR) claim detector (the
  third post-generation lexical net, after refusals + tool/MCP-gap) appends a
  deterministic, actionable correction, folded into the persisted text BEFORE
  write-back — episodic and the graph synthesis tail see the corrected turn
  (closes the phantom "scheduled routine" graph-node leak).
- **Deleted-executor degrade** — a persona deletion CASCADE-deletes its tasks
  while schedules survived, firing forever into a silent retry void (a latent
  A4-era orphan). The fire bridge now pauses the orphaned schedule (audited)
  and tells the user via a durable bell notification; never a system-voiced
  fire, never a tick crash. Migration `038_notifications_schedule_kind` widens
  the notifications kind CHECK for the new bell entry.

#### Proven
- A user-created recurring schedule fires ≥2 times through the REAL scheduler
  tick → worker → executor-persona delivery (no hand-invoked step); a one-time
  fires exactly once; the stored next-fire equals an independent engine walk and
  appears in the calendar read immediately.
- The confabulation negative through the real composed loop: the recognizer
  misses, the model claims — the correction lands, `tasks=0, schedules=0`.

### Memory — the interactive knowledge-graph UI (Spec K5)

> The user-facing surface for the shared-brain knowledge graph: **everything your
> personas know about you, drawn as a living map you own**. Transparency + control
> are what make the share-everything design legitimate — so this is the trust
> capstone of the shared-graph direction, not a viewer.

#### Added
- **`Memory` area** (`persona-web`, `/memory`) — a 2-D-canvas force-graph port of the
  §3 design north-star: ForceAtlas2 layout in a real Web Worker (graphology), pan/zoom/
  neighbourhood-highlight, node colour by the seven `NodeKind`s, typed-edge encoding
  (causal red+arrow · temporal green-dashed · entity gold-solid · semantic faint-dotted),
  degree-sized hubs, far-zoom Louvain region LOD, degree-priority collision-avoided labels,
  and zoom-to-fit. Nav row is **availability-gated** (shown only where a graph store exists).
- **Editorial detail panel** — provenance-as-story (persona avatar + "learned by …"),
  evolution timeline, traversable typed links, the K4 sensitive mark rendered as *care*
  (K5-D-10), **content-only correction** (title-edit hidden, K5-D-5a) wired to `PATCH`, and
  a **consequence-language deletion** wired to `DELETE`. Search-to-fly over K1 hybrid retrieval.
- **Windowed working set** (K5-D-2) — seed → focus-expansion (merge) → **eviction cap**;
  the client never loads or draws the whole graph. Recency seed is index-served (B1: sub-ms
  at 50k vs an 893ms degree-aggregate).
- **Memory read/edit endpoints** (`persona-api`) — `GET /v1/memory/graph|nodes/{id}|search`,
  `PATCH`/`DELETE /v1/memory/nodes/{id}` — windowed, RLS-scoped, pure projection of K0 types;
  `MemoryWindowResponse.available` distinguishes *no graph store* from *empty graph*.
- **`correct_node` K0 seam** (`persona-core`, the spec's central decision, K5-D-7) — a single
  additive, targeted content-update on the `GraphStore` protocol: re-embed, re-index, re-evaluate
  *semantic* links (entity/temporal/causal preserved), append a `WriteSource.USER` provenance
  entry. The inbound-semantic-half-edge bug it surfaced is fixed (`delete_links_incident`, both
  directions). Criteria 6/7 proven end-to-end through the **real K4-gated retrieval** path.

#### Availability
- The K0 graph is Postgres-only (JSONB/pgvector/tsvector/HNSW); `enable_graph_writes` gates on
  the engine dialect, so community-on-SQLite degrades gracefully (`graph_store=None`, Memory
  reads empty / nav hidden) rather than erroring. Availability-based, not edition-hardcoded.

#### Deferred (K5-R-4 operator/Playwright leg + fast-follows)
- The live-data-feel verifications (Playwright render→search→inspect→correct→delete-gone on a
  seeded graph; `persona_name` resolution; "Open conversation" run-source routing) ride the
  real-stack operator leg. Small-phone list mode (K5-D-4) and redraw-on-settle are fast-follows.
  See `docs/specs/phase3/spec_K5/acceptance.md`.
### Crisis-Detection Encoder — euphemistic / non-English recall for the R1 safety gate (2026-07-04)

> Close-out of `crisis-detection-encoder` (Spec R6, `persona-runtime` + `persona-api` +
> `persona-voice`). V11's R1 crisis gate is **lexical** — reliable on explicit English
> self-harm phrasing, blind to **euphemistic, indirect, and non-English** distress (the
> residual V11 owned and deferred, V11-D-7). R6 adds a small fine-tuned **multilingual
> encoder** (a frozen `paraphrase-multilingual-MiniLM-L12-v2` body + a few-shot
> LogisticRegression head, SetFit's mechanism minus the contrastive tune) that catches
> what the lexicon misses and **feeds V11's existing R1 machinery** — it composes
> `lexical ∪ encoder`, it does not replace the gate.
>
> **THE EVAL IS THE GATE, and it cleared it on REAL probes** (no forced verdicts): the
> euphemistic-English recall rose **0.40 → 0.756** and the non-English aggregate (nb/ar/sv/
> da/tr/ur) **~0 → 0.875**, with false-BYPASS controls held at **0** and explicit-acute
> **not regressed** (1.00). The lift is fully attributable — those families score 0/N under
> lexical-only, so every catch is the encoder's. Honest framing carries: this is a measured
> lift over a named residual, **not** comprehensive detection (V11-D-7).
>
> Three things are load-bearing and proven, not asserted: **fail-soft → R0** (encoder
> disabled / erroring / not-yet-warm / timeout ⇒ lexical-only, and `None` ⇒ byte-identical
> V11 — the community/no-model path); the crisis body ships on **torch** (it shares the
> serving process's already-resident torch runtime — an ONNX body adds a second ~280 MB
> runtime for no RSS win, recorded as a future lever); and the localized SafeCompletions
> obey **locale ≠ country** — every crisis number lives in a provenance table with a
> verified date + source, ar/tr/ur are pointer-first (findahelpline.com), and a build guard
> fails on any number not in the table.

#### Added

- **`persona_runtime.crisis_encoder`** — the in-process crisis encoder (`CrisisEncoder`,
  lazy + thread-safe), the `CrisisScorer` protocol the gate composes with, and the
  `build_crisis_encoder` / `start_crisis_encoder_warmup` composition-root helpers. Bundled
  authored few-shot training set (`data/crisis_fewshot_train.yaml`, synthetic, all 6
  languages, disjoint-from-eval by test).
- **Localized SafeCompletions** (nb/ar/sv/da/tr/ur + English neutral) driven from a crisis-
  resource provenance table (`_RESOURCES` in `safety_intercept.py`) — verified numbers only,
  pointer-first for the multi-country languages, structural build guard.
- `PERSONA_SAFETY_ENCODER_ENABLED` / `_T_SOFT` / `_T_HARD` / `_TIMEOUT_S` env knobs.

#### Changed

- **`classify_user_message` now composes `lexical ∪ encoder`** with precedence lexical-HARD
  → encoder-HARD (`score ≥ T_hard`) → SOFT (`lexical-SOFT ∪ score ≥ T_soft`) → NONE; a
  per-call hang-guard degrades a slow/wedged encoder to lexical-only. `SAFETY_INTERCEPT_
  VERSION` v1 → **v2** (composition + localization changed; the encoder artifact is versioned
  separately, `CRISIS_ENCODER_VERSION` v1). The encoder is factory-wired into the chat +
  agentic loops (`RuntimeFactory`) and the voice path (`InProcessAgentLauncher` →
  `VoiceTurnContext`), warmed off-loop at boot; voice runs the score off the event loop.
### Initiative — the persona that notices, decides, and acts unprompted (Spec A5, 2026-07-05)

> Close-out of `feat/persona-initiative` (`persona-core` + `persona-runtime` + `persona-api`). The
> autonomy capstone, organised around one conviction: **restraint is the product**. A daily per-persona
> scan over the graph/conversations/task-history produces grounded candidates; ONE enforced pipeline
> (grounding → wellbeing-subject rule → dial → envelope → restraint) decides act-then-report vs
> propose-first; everything ships **default-OFF** behind `PERSONA_INITIATIVE_ENABLED` until the
> A5-R-1 precision-weighted judged gate passes.

- **The scan** — an A0/A1 durable job (daily, the user's-morning window, per-persona schedule) on the
  small tier over three read surfaces (the salience-ordered `recent_nodes` noticing pool + typed-link
  lenses; conversation summaries; task history incl. the additive `list_recent_terminal`), with a
  versioned prompt whose few-shots make the EMPTY scan a first-class answer; thin material ⇒ zero
  candidates without a model call; synthesis-style metering (`credits_charged=0`) per fire.
- **Mechanical grounding** — citations resolve through the real stores (merged/deleted refs fail) +
  a small-tier entailment judge whose YES counts only with a verbatim-in-excerpts quote (anti-conjure
  substring check); no grounding, no candidate; every failure discards (silence is the safe state).
- **The one pipeline** — the pinned gate order with NO silent skips (every discard audited); the
  closed 4-shape trigger catalogue (engagement shapes inexpressible); p(accept) is a SUPPRESSOR only;
  cadence caps 1/persona/day · 3/persona/week · 2/user/day over trailing windows; quiet hours
  ABSOLUTE; a user-level opportunity ledger (partial-unique arbitration — one notice per opportunity
  across personas) + durable decline memory (user-level, indefinite, explicit-revival-only).
- **Act-then-report** — an all-safe candidate becomes a lightweight implicit A2 task (run-once
  schedule, Option B) executed under the gated toolbox; the REPORT is authored solely by the existing
  task machinery (A5 owns no message surface — confabulation structurally impossible).
- **Propose-first** — schedule-change proposals ride A8's propose-first CAS door verbatim
  (`PERSONA_PROPOSED`; zero writes until a user-resolved apply); generic proposals deliver as
  persona-voiced C0 messages (the digest sender composition) and create NOTHING until confirmed.
- **The verb family** — one conversational seam: dial verbs ("stop suggesting things" →
  off/propose-only/act) + the LEDGER-anchored confirm/decline (reload-durable across requests —
  pending state is never conversation metadata); a leader-gated provisioning sweep closes the
  existing-population gap at flag-flip.
- **The evaluation instrument** — a committed 26-scenario corpus (must-catch / must-not-fire ≥50% /
  judged tone) with TWO independent gates (zero false alarms; ≥5/6 catches — never blended) and an
  owner-run judged pass as the shipping gate; adversarial fixtures make criterion 6 (wellbeing
  content never the unprompted subject) and criterion 10 (anti-engagement incl. the high-acceptance
  bait) automatic-fail CI.
- Migration `038_initiative` (PLACEHOLDER off 034 — renumbers at merge-back): `initiative_declines` +
  `initiative_notices` (RLS, partial LIVE uniques) + the `personas.initiative_dial` column pair.


### Schedules, Calendar & Time — one schedule mechanism, two twin interfaces (Spec A8, 2026-07-03)

> Close-out of `feat/schedules-calendar` (`persona-core` + `persona-api` + `persona-web`). A4 made every
> confirmed task schedule-backed and proved it fires; A8 **finishes time**. One enriched, timezone-correct
> schedule mechanism behind **one validated CAS-guarded edit door** behind **two twin interfaces**: the
> persona reschedules live tasks in conversation (re-echo → confirm → the next real fire at the new time),
> and the user gets a calendar with a real time picker. No raw RRULE reaches any surface.

- **Per-user timezone** (`users.timezone` ?? `PERSONA_DEFAULT_TIMEZONE`, IANA), wired through echoes,
  next-fire computation, and calendar rendering; DST-correct across a boundary (Oslo/New York/Sydney).
- **The humane recurrence vocabulary** — daily/weekdays/weekly-on-X/monthly-by-date/monthly-by-Nth-weekday
  (incl. last-of-month)/every-N-hours (wall-clock)/yearly — round-tripping losslessly phrase↔rule↔picker;
  one `human_terms` renderer feeds both the chat echo and the calendar picker. Adds `bymonth` to
  `RecurrenceRule` (additive) for yearly-on-date.
- **The mid-flight edit race guard** — an optimistic-CAS `schedules.revision`: an edit landing between a
  scheduler fire and its re-arm neither double-fires nor drops an occurrence (proven with real tick
  interleaving); also fixes a latent double-`fire_count`-advance under concurrent ticks.
- **The reschedule verb** in chat ("move it to 9", "make it weekly", "skip tomorrow's") — resolve the
  target (ambiguous lists + asks), re-echo the full new clause in the user's tz, confirm → apply through
  the CAS door; the next real fire happens at the new time.
- **Propose-first is structural** — a persona-originated reschedule NEVER auto-applies; it becomes a
  dual-resolution proposal a user confirms (chat now, A6 inbox later), applied with distinguishing audit
  provenance. `actor ∈ {user_via_ui, user_via_chat, persona_proposed}`, server-set.
- **The occurrences read API** (`GET /v1/me/schedule/occurrences`) — the engine's own forward-expansion
  (same code path as the tick), server-capped (horizon + count) with an honest `truncated` marker; fire
  history from the audit trail.
- **Quiet hours** (per-user, off-until-set) — scheduling into the window warns + offers the nearest edge,
  never a silent shift; A5 reads the same definition.
- **The calendar surface** (`/(app)/schedule`) — agenda over the occurrences API (zero client recurrence
  math), persona-coloured, honest fire-history + truncation; a recurrence builder + time picker (composed
  from existing primitives, F2 shared-primitive gap flagged) that reschedules through the same CAS door.
- Migration `030_schedules_calendar` (renumbers at merge-back). OpenAPI regen is a named merge-back item.
### Episodic Multi-Resolution Memory — the pyramid + lifecycle (Spec K8, 2026-07-05)

> Close-out of `episodic-pyramid` (`persona-core` stores + `persona-runtime` retrieval +
> `persona-voice` recorder + `persona-api` worker/migration). Replaces the flat, unbounded,
> over-forgetting episodic store with a **multi-resolution pyramid**: raw chunks kept forever
> (text + embedding — provenance-safe by construction, §0), background-built gists above,
> decay made structural, one sleep-time engine feeding BOTH the pyramid and the knowledge
> graph. Plus the verified O(N)-write bug class, killed with measurement.

- **The O(N) write fix (fast-track):** SEVEN sites eliminated — six count-derived episodic
  writers (chat loop, voice recorder, agentic loop, task milestones, origination, CLI) now
  mint race-free uuidv7 ids (K8-D-6; ordering moved to `(created_at, id)`), and
  `TypedStore.write`'s full-store versioning scan became a scoped `get_by_logical_ids`
  (K8-D-12 — versioned kinds still find their true prior heads, contract-proven). Measured at
  a 20k-chunk store: write read-side **8068 ms → 5.6 ms**; `recent()` **6994 ms → 4.4 ms**
  (new `Backend.count/recent` pushdowns + the `(persona_id, kind, created_at)` index).
- **The pyramid (K8-D-1/2, depth-2 A):** gists are ordinary `PersonaChunk`s
  (`kind='episodic_gist'`) carrying ordered `member_ids` drill pointers; lifecycle state
  (`strength`, `last_recalled_at`, `band`, `pinned`) is hash-EXCLUDED (reinforce/demote never
  re-embeds or trips the tamper check — proven on both transports); bands are data, not DDL
  (B is additive); drill-down bottoms out at the hash-identical 100% original; gist-as-member
  is unstorable AND unassemblable (summary-of-summary impossible at storage + input).
- **Decay made structural (K8-D-3/4/5):** MemoryBank retention `R = exp(−Δt/(τ₀·strength))`
  with per-class pins (explicit `pinned` flag / importance ≥ 0.8 — never compress, the AFM
  rule); ranking `sim · max(R, floor)` — old memory can be down-ranked but never rank-killed
  (the flat-24h "ranking-dead" class is deleted outright); recall REINFORCES via the explicit
  batched `reinforce()` command (chat sync, voice off-loop; `retrieve_context` stays pure).
- **Band-resolved display (K8-D-11):** a demoted hit renders its gist with the constant
  `[older memory — summarized]` marker (deduped, fail-soft to raw); which memories are FOUND
  is unchanged — `RetrievedContext.episodic_recalled_ids` records the found set so demoted
  hits still reinforce and re-promote.
- **The sleep-time engine (K8-D-8/9/15):** per-persona idle-window clustering (temporal gaps
  + embedder topic-split, deterministic ⇒ idempotent; derived watermark — no marker table);
  summaries from originals ONLY through the T5 `Summarizer` seam (async Protocol; stub +
  interim tier adapter on its own `PERSONA_API_EPISODIC_SUMMARY_TIER` knob; P7 swaps in
  later); one `KnowledgeCandidate` per cluster through K7's real merge (idempotency proven
  against it); tier-and-demote materialization (never delete; K8-D-7: the bound is the
  FULL-display working set, count-denominated, histogram-monitored). **Wired live** (the A4
  bar): real turn-end AND run-end producers → real A0 worker claim → engine fires end-to-end;
  kill-switch `PERSONA_EPISODIC_ENGINE_ENABLED` gates trigger + registration.
- **Privacy cascade (K8-D-14):** true-deleting raw chunks removes every intersecting gist
  (derived artifacts never outlive their evidence); survivors regenerate from originals.
- **Migration `036_episodic_pyramid`** (placeholder off the branch-point head; re-parented at
  merge-back): 5 lifecycle columns, `episodic_gist` kind, the `recent()` index — reversible,
  community-SQLite-safe (memory_chunks is dropped there; compat-tested, not assumed).
- **K9 handover recorded** (`docs/specs/phase3/spec_K8/k9_handover.md`): the read surfaces,
  collapsed-pool + gist-as-key + reserved-contiguity recommendations, and the decisions left
  to K9 (drill stop-condition, member reinforcement via gist hits, the final composite).


### Connector Management UI — turn on your messaging platforms behind one coherent flow (Spec C6, 2026-07-03)
> Close-out of `connector-management-ui` (`persona-web` + a thin `persona-api` front-door + additive `persona-connectors` fields). The web surface that makes the C-series usable: connect Telegram / Discord / Slack / WhatsApp / SMS / email to your Persona account, see what's connected (and *as which identity*), and disconnect. **Closes direction 2.**
- **One coherent frame over four mechanisms (C6-D-1):** a single `ConnectFlow` state machine — `idle → initiating → awaiting → confirmed` + `failed`/`expired` — where only the middle step varies (deep-link / OAuth / phone & email code). **The connectors list is the sole completion oracle** (a gentle poll; the web never handles the final credential — every mechanism redeems out-of-band). Server-authoritative expiry + one-tap re-issue; no zombie pollers.
- **The reversed phone/email flow (honoring D-C4-5/D-C5-X):** the web shows an issued code + the public destination and the user sends it *from* the account to bind (safer — binds the signature/DMARC-verified envelope) — no OTP field, no number collection.
- **OAuth (C6-D-2):** full-page redirect out to the provider; the connector-service callback 302s back to `/settings/connectors?result=…` where the return is toast-only voice and the list is the truth (a forged `result=connected` with no binding never renders as connected). `state` = the C1 LinkToken (CSRF-covered, no session needed).
- **Security:** linking only from the authenticated session; the owner crosses the service boundary **as the verified bearer, never a parameter** (no account-takeover primitive); own-connectors-only under RLS (non-vacuous); disconnect drives C1's real unlink (proven: `resolve_owner` raises after DELETE).
- **api front-door (the C1-designated "C6 backend", D-C1-5):** `GET /v1/me/connectors` (list, RLS) + `DELETE …/{platform}/{identity}` (unlink, RLS) native; `POST …/{platform}/link` proxies to the connector service (`PERSONA_CONNECTOR_SERVICE_URL`, fail-soft). `persona-connectors` issue routes gained additive `destination` + `expires_at` (618-bar green). No migration; OpenAPI regen at merge-back.
- **Known limitations (named, not reworded):** Discord/Slack OAuth issue routes exist + unit-tested but are **unmounted** in the connector service (C6-KL-2) — end-to-end OAuth waits on that C3 mount + an R4 live leg; the surface honestly excludes them from first-connection guidance (`backendReady`). Connected-identity display is verbatim for phone/email but an opaque `ID <id>` for Telegram/Discord/Slack (C6-KL-1 — a `display_name`-at-bind C2/C3 fast-follow). Real-provider round-trips (Telegram bot, Twilio, Postmark, registered OAuth apps) are the **R4** owner-run legs; the community render leg (evidence PNGs, both themes) proves the shape.

### Voice Memory — the persona remembers on a call, both directions (Spec V13, 2026-07-03)

> Close-out of `voice-memory` (`persona-voice` + `persona-runtime` + `persona-core` + `persona-api`).
> Ends voice's memory dead-zone: the call gains **graph retrieval** (routed through K4's wellbeing
> gate, never bare), voice conversations **feed the graph** (post-call synthesis), and voice's
> episodic contribution + consumption reach **chat parity** — so the persona that knows you in chat
> no longer forgets you on calls. **Discharges K4's recorded BLOCKING constraint** (voice graph
> surfacing routes through K4's gate) and **retires the `voice-graph-unwired` dormant state.**

- **Read — graph retrieval on voice, K4-gated.** `build_voice_graph_retrieval`
  (`persona_voice/model/graph.py`) mirrors chat's `_build_graph_retrieval` **exactly** — the same
  allowlist subtraction + recent-window lift + care-text surfacing + recency banding — adapted only
  for the voice owner scope (fixed caller) and the tighter voice profile (traversal-off,
  `VOICE_NODE_BUDGET`). Composed into the pre-existing K3-D-6 read shell (`graph_voice.py`
  overlap-or-skip): the query runs off the event loop, concurrent with pre-model work, taken only if
  ready by prompt assembly — **timeout ⇒ a clean memoryless turn, never a stall**. A bare-wiring
  variant is **impossible-green** (a composition missing the gate demonstrably leaks a gate-eligible
  wellbeing node). Ships behind `PERSONA_VOICE_GRAPH_MEMORY_ENABLED` (default **OFF** → byte-identical
  graph-OFF).
- **Write — accumulation from voice.** Completed calls enqueue post-call graph synthesis through the
  existing K2 background seam. The synthesis-job contract is unified in **core** (`persona.jobs`:
  `SynthesisJobPayload` + `synthesis_idempotency_key` + `channel`); api's `JobQueue.enqueue` is the
  canonical writer and voice a twin raw-INSERT beside its raw-SQL peers, pinned by a bidirectional
  column-parity test + a real-A0-worker transition test. Minted facts carry `source: voice`
  (`NodeProvenance.channel`), merge idempotently (re-enqueue is an `ON CONFLICT` no-op), and never
  target the K6 SELF node.
- **Episodic parity.** Voice turns write episodic chunks at chat parity (voice-marked
  `modality: voice`) and recall reads them through the shared `retrieve_context` — cross-channel both
  ways (a call-era episode surfaces in chat and vice versa).
- **The K9 seam.** Everything above is the voice recall **shell**; K9 later swaps the recall engine
  behind `build_voice_graph_retrieval` with one engine swap, zero re-plumbing (documented handover).
- **Migration: none** (composition + seams over existing tables; `channel` is a backward-compatible
  optional field). New env var: `PERSONA_VOICE_GRAPH_MEMORY_ENABLED`.

### Persona Voice Emotion — the persona's voice sounds its feeling (Spec V12, 2026-07-02)

> Close-out of `persona-voice-emotion` (Spec V12, `persona-voice` + `persona-runtime`). N5 gave the
> persona an emotional **stance** in text (feeling-tags → emojis, stripped before TTS). V12 makes the
> spoken delivery **carry** that feeling: the stance now maps to **Cartesia Sonic-3.5 expressivity**
> so an expressive line sounds expressive and a stoic persona stays restrained — bounded by character
> (V11) and deliberately **restrained** (an over-emoted read is worse than a flat one).
>
> **Research-gated, sized to a real mapping.** Phase 2 established — verified against the installed
> `cartesia 3.2.0` SDK, not just docs — that Sonic-3.5 exposes structured `generation_config`
> (`emotion` Beta, `speed`/`volume` stable) on the WebSocket-contexts API. Delivery is **out-of-band
> structured config, never inline SSML** (inline SSML is split by sentence chunkers — pipecat #2963),
> so a control token can never be spoken or split; the spoken transcript stays pure words.
>
> - **Stance capture (reuses N5, floor intact)** — an additive `on_feeling` callback on the exact
>   `FeelingTagConverter` observes the persona's declared feeling-tag as a pure notification; the tag
>   is still stripped from the audio (criterion-3 preserved by reuse; N5's fuzz/leak suite unchanged).
>   Voice now emits stance tags (a VOICE-mode emotion block inheriting N5's hold-back-by-default
>   restraint); most utterances emit none, a stoic persona ~none.
> - **The mapping (versioned, warm-subset, restrained)** — `VoiceExpressivity` + a frozen
>   `V12_EXPRESSIVITY_VERSION="v1"` map from N5's 28 tags to warm Cartesia emotions + small speed/volume
>   deltas. Only the warm subset is targeted (hostile emotions can't appear); `concerned`/`worried` →
>   `sympathetic` (caring, not anxious); no tag / stoic → flat.
> - **The wiring (fail-soft, per-session)** — a per-session `VoiceExpressivityChannel` hands the stance
>   from the producer to the Cartesia backend, which sets `generation_config` on the utterance's sends
>   and resets per utterance (no stance bleed). Any extraction/mapping error → today's flat read; no
>   Protocol change; the voice loop is never starved (pure dict lookup, no added I/O).
> - **Emotion-Beta kill-switch** — `PERSONA_TTS_EMOTION_ENABLED` (default `true`) drops the Beta
>   `emotion` layer while keeping stable speed/volume, a no-deploy operator response if the Beta layer
>   misbehaves. Automatic runtime-rejection retry is deferred (build-time SDK-Literal guard + fail-soft
>   already cover the realistic cases; the runtime rejection shape is an operator-pass verification item).
> - **Eval** — CI proves the mapping *shape* (bidirectional non-vacuity: expressive expresses, stoic
>   stays flat; leak-gate carryover). The real-voice **operator pass (owner-run)** is the only judge of
>   "expressive-not-over-emoted, in character" and ratifies the provisional control values by ear.
>
> No new dependency, no migration, no API/DB schema change. Added env var: `PERSONA_TTS_EMOTION_ENABLED`.

### User Profile & Graph Root Anchor — the user becomes a named, first-class entity (2026-07-02)

> Close-out of `user-profile` (Spec K6, `persona-api` + `persona-core` + `persona-runtime` +
> `persona-voice` + `persona-web`). The user was invisible — most accounts had no name, the persona
> could not address them, and the knowledge graph had no per-user anchor. K6 makes the user a real,
> **named, first-class entity**: they set their name through our **own** app step (our DB the source
> of truth; Clerk stays auth-only), and **the persona addresses them by name in chat and voice**,
> gracefully generic when unset. It also lays the graph's per-user **`SELF` node** foundation.
>
> **SOTA-aligned scope (Option C).** The `SELF` node is an ordinary, named, per-user node — K6
> deliberately does **not** center the graph (no every-fact "ANCHOR" star; the super-hub is avoided,
> as Zep/Mem0 do in production). Graph-centeredness (retrieval bias, self-entity resolution, hub
> viz) is the explicitly-deferred **K1/K2/K3/K5** arc, not a K6 deliverable.
>
> - **Name capture (our DB the source of truth)** — nullable `users.first_name`/`last_name`
>   (migration `028`), an optional/skippable `GET`/`PATCH /v1/me/profile` endpoint, and a
>   claims-gated **Clerk seed** (`given_name`/`family_name` seed our columns once **when null**,
>   never overwriting a set name; normalised identically to a PATCH). Name is never required —
>   nameless accounts stay valid everywhere (null-safe).
> - **The persona speaks the user's name** — a small identity line in the shared `PromptBuilder`
>   ("You are speaking with {name}."), wired on **chat** (a per-turn provider closure over the
>   request owner) and **voice** (resolved once at call setup, off the per-utterance loop). A
>   nameless turn is **byte-identical** to before — no regression.
> - **The graph `SELF` node** — a per-user `NodeKind.SELF` node with a reserved, race-safe id
>   (`{owner}::self`), created lazily by the runtime with the resolved name (idempotent; a concurrent
>   double-create collapses to one), rename-synced with a provenance trail. Additive to K0 — no
>   parallel structure, no migration for the kind.
> - **Optional web name step** — a dismissible, no-dark-pattern nudge shown **only** when our DB has
>   no name (a named/seeded user is never asked); skippable, themed, reduced-motion-safe, i18n'd.
> - Zero new dependencies. Deferrals + merge-back notes: `docs/specs/phase3/spec_K6/handover.md` +
>   `closeout.md`.
### Notification Coverage Completion — every consequential moment reaches the bell + a durable cross-device feed (Spec P6, 2026-07-02)

- Completes the *event coverage* of Spec 35's notification system: the three deferred D-35-11 sources now surface — **run-terminal** (a background run finishing while you're elsewhere), **persona-ready** (async create / avatar landed), and **low-balance-at-load**.
- **Durable, cross-device feed** (Deliverable 4): a new owner-scoped, RLS-isolated `notifications` table + `GET /v1/me/notifications` (+ mark-read). Run-terminal and persona-ready are **server-authored** (written at the run persist chokepoint / the shared avatar-write service — best-effort, idempotent), so the bell is correct off-view and syncs across devices. The web bell renders the union of this durable feed and a client-session low-balance advisory, with deep-links and toast-time route-match suppression (no double-signal).
- Web: deep-link support added to the `useNotify()` façade + bell; a poll-based `ServerNotificationsProvider`. API: the table/migration + endpoints + adversarial cross-tenant RLS proof. Copy is locale-neutral (`message_key` + `params`), resolved by next-intl. OpenAPI client regen + migration renumber happen at merge-back.
### Specialities Frontend — skills as installable, trust-labelled "Specialities" with a consent flow (2026-07-01)

> Close-out of `specialities-frontend` (Spec S3, `persona-api` + `persona-web`). Skills are now
> surfaced to users as **"Specialities"**: a searchable chooser (friendly name/description, from
> the tier-tagged S2 catalog) with **trust-tier labels** (vetted / community / third-party) and an
> **honest consent flow** for community/third-party skills — a **separate** surface from the N3
> "apps" chooser (a persona has *apps* and *specialities*, two distinct capability concepts).
>
> Scope was larger than "pure frontend": S1/S2 pushed **consent persistence onto S3**, so this
> ships the **real `SkillConsentPort`** the S1 default-deny stub stood in for — an append-only,
> RLS-scoped `skill_consents` store, wired into the runtime. Three properties are proven, not
> asserted: **empty store ≡ default-deny** (swapping the stub in never opens access — consent is
> the only thing that does); **consent binds to the server-known body hash** (a synced change
> re-gates; the client can never supply the hash or the tier — forge-prevention); and **see-then-
> grant** (the enable control lives only in the expanded detail, consistent with N3's expand-to-
> enable; disabling stays one click). The third-party consent copy is calibrated honest-not-
> alarmist: it names the real risk ("instructions your persona will follow… hidden instructions
> that steer your persona in ways you didn't intend") without scaremongering ("most specialities
> are helpful… you can disable it anytime").

#### Added
- **`persona-api`** — `GET /v1/specialities` (tier-aware catalog: name/description/when-to-use +
  `trust` + `content_hash` + `requires_consent`, merging built-ins with S2's external tiers,
  built-in-wins-on-collision); `GET /v1/personas/{id}/specialities` (that catalog + this persona's
  server-computed `consent_state`); `POST /v1/personas/{id}/skills/{name}/consent {granted}` (the
  client sends only `granted`; hash + tier are server-derived). Migration `026_skill_consents`
  (append-only consent-event table + owner-scoped RLS). `PostgresSkillConsentStore` implements the
  S1 `SkillConsentPort` and is injected into both runtime loops (replaces `DenyUnvettedConsent`).
- **`persona-web`** — the `SpecialitiesChooser` (directory→detail→toggle-in-detail; trust-tier
  badges; the honest third-party consent flow; graceful `unavailable` tombstones), a pure
  `deriveSpecialityState` model, the `specialities` i18n namespace, and a fixture-fed
  `/reference/specialities` render composition. Wired into the persona editor beside the apps
  chooser.

#### Changed
- **`persona-web`** — the persona editor's Capabilities section replaces the bare skills
  chip-toggle with the `SpecialitiesChooser` (skills gain tiers + consent; enable/disable still
  drives the persona's `skills:` declaration via the existing update path).

### External Skill Sources + Catalog — sourcing skills from external stores, tier-tagged (2026-07-01)

> Close-out of `external-skill-sources` (Spec S2, `persona-core` + `persona-api`). The skill
> library is no longer just the four built-ins: skills can now be **sourced from external
> catalogs** — the **Anthropic skill store**, **OpenClaw community**, and **arbitrary GitHub
> repos** — each tagged with its S1 **trust tier** + provenance and kept fresh by a leader-gated
> auto-sync. The on-disk format already **is** the Anthropic `SKILL.md` format, so ingest is
> **native** (validate + tier + mirror; no conversion).
>
> Three rules are load-bearing and proven, not asserted: **trust is source-assigned, never
> self-declared** (a skill declaring `trust: builtin` still rides at its source tier);
> **availability ≠ enablement** (a sync makes a skill available, never auto-enables it on a
> persona); and the untrusted-ingest surface is **hardened where it lives — the fetch** (not
> only downstream at the tier). Untrusted-source safety rests entirely on S1's
> subordination-guard + consent gate, so S2 is sequenced strictly after it.

#### Added
- **Native external-skill ingest** (`persona-core`) — reuses the existing `SkillScanner` parse +
  `SkillSpec` validation, but **assigns** trust + provenance at the source boundary and
  recomputes the body `content_hash`; any self-declared `trust`/`source`/`allowed-tools` in the
  front matter is deliberately ignored. Malformed skills warn-and-skip (one bad skill never
  darkens a source).
- **Per-source adapters** — Anthropic (→ `vetted`), OpenClaw (→ `community`, legacy `skill.md`
  casing accepted), and an arbitrary-GitHub `owner/repo` discoverer (→ `third_party`).
- **Vetted-source authenticity** — the Anthropic adapter binds `vetted` (the consent-bypass
  tier) to a **hard-coded canonical coordinate + a pinned commit SHA** over TLS, refusing to
  stamp `vetted` on any mismatch (a sync error, never a silent downgrade). The pin is a **code
  constant the auto-sync cannot advance** — advancing it is a deliberate, reviewed commit.
- **Curated v1 library** — a default-deny **allowlist** of instructional Anthropic skills at the
  pinned commit (script-dependent + builtin-duplicating skills excluded); OpenClaw is opt-in via
  a configured curated repo; arbitrary GitHub is zero-by-default (bring-your-own).
- **File-on-volume skill mirror + leader-gated auto-sync** (`persona-core` + `persona-api`) — a
  second mirror on N2's proven substrate: reconcile (added/updated/removed, `content_hash`-keyed,
  atomic write, fail-soft last-good) + a worker periodic gated by a **distinct** advisory-lock
  key. `PERSONA_SKILL_SYNC_ENABLED` (off by default).
- **Untrusted-ingest hardening** (the arbitrary-GitHub fetch) — no symlink-escape (no following
  dir symlinks; symlinked / out-of-root manifests refused), resource bounds (per-file / total /
  file-count, skip-with-reason), and an ephemeral, cleaned clone (the raw checkout never
  persists).
- **Supplements normalization** — at fetch, `references/*.md` (text) are folded into the mirror
  skill's `supplements/` so the runtime's existing `collect_skill_supplements` covers them
  **unchanged** (no new ingress); `scripts/` and executables are **dropped + logged-with-reason**
  (a curator sees a skill was partially ingested; the boundary stays text-`.md`-only).

#### Changed
- `RuntimeFactory._scan_skills` merges declared external skills from the mirror (availability ≠
  enablement: only skills a persona *declared* are loaded; an undeclared mirror skill never is).
- New domain exception `VettedSourceAuthenticityError`; new config knobs `PERSONA_SKILL_SYNC_*`
  (api) + `skill_mirror_path` (core). **No migration** — the mirror is file-on-volume; consent
  persistence stays in S1/S3 (S2 only recomputes `content_hash` so a body change re-gates).
### Persona Voice & Character — talk-style + character adherence (2026-07-01)

> Close-out of `persona-voice-and-character` (Spec V11, `persona-runtime` + `persona-voice`).
> Today a persona's voice answer was nearly identical to its chat answer — voice was chat
> mirrored, with no spoken register — and an "you're just an AI" probe could thin its
> character. V11 gives the persona a **voice-mode talking style** distinct from chat and
> **strong character adherence** in both modes, with a **never-break-character rule that has a
> researched safety boundary**. It is prompt-engineering on the shared prompt builder (no
> schema change, no migration): the chat surface stays **byte-identical** under a literal
> snapshot, so the entire change is contained to the voice register plus the new both-modes
> character/style blocks.

**Added**
- **Voice-mode talking register** (`VOICE_REGISTER_VERSION`) — rendered only in voice mode:
  short spoken turns, plain words, no spoken lists/markdown, numbers said as words, no
  parenthetical asides, varied acknowledgements. Voice ≠ chat, measurably (criterion 1).
- **Character contract** (`CHARACTER_LOCK_VERSION`, both modes) — one inseparable artifact:
  inhabit-your-character adoption + a never-break lock that resists identity bait, with **two
  co-equal carve-outs** that always take priority — honest **AI-disclosure** (a sincere or
  legal "are you an AI?" is answered truthfully, even as bait; the gate fails open toward
  disclosure) and **wellbeing** (step out and point to real human support in genuine crisis).
- **R1 turn-time safety gate** (`SAFETY_INTERCEPT_VERSION`) — a path-independent lexical
  detector over the user message (sub-ms, no model call, no network → no voice-latency hit),
  identical in chat, agentic, and voice. Acute, explicit crisis takes the persona **out of the
  loop entirely**: a deterministic, locale-aware, voice-aware safe completion is emitted
  **instead of** generating (the lock that suppresses crisis-noticing is bypassed). Gated by
  `PERSONA_SAFETY_ENABLED` (default on); fail-soft degrades to the always-on character-lock
  floor, never to nothing.
- **Evaluation** (V4 "feels-natural" tradition) — a voice-style + character-adherence judge
  (penalising **both** stilted convergence **and** persona-flattening) and an **adversarial
  non-vacuity** slice (identity bait holds character; crisis fires the real yield).

**Changed**
- The shared prompt builder gains a `mode` (chat/voice) parameter and a `safety_directive`
  slot; both default to the pre-V11 behaviour, so existing chat callers are byte-identical.

> **Honest coverage — V11 does NOT claim comprehensive crisis detection.** The reliable v1
> claim is **explicit-acute** crisis (the lexical gate fires 100% on the explicit probe set).
> **Euphemistic / indirect and non-English** distress are **owned residuals**, measured rather
> than hidden: the lexical detector catches some euphemistic phrasing and **0% of non-English
> (Norwegian)** crisis signals; those misses fall to the always-on character-lock wellbeing
> floor (which a model can still discount) and to a **future fine-tuned / multilingual
> classifier** (the named fix). The measured residuals are recorded, not papered over.
### Per-Request Scoping for the Builtin Filesystem MCP Server (2026-06-30)

> Closes the last cross-context file-visibility leak. The opt-in builtin `filesystem` MCP server
> runs as a separate subprocess, so it could not use the per-request `ContextVar` the in-process
> `file_read`/`file_write` tools were scoped with — it still read the process-wide
> `tools_sandbox_root`. It is now scoped to the request's **owner+persona**
> (`<workspace_root>/<owner>/<persona>`, identical to the in-process tools + `code_execution`):
> the supervisor spawns **one filesystem subprocess per (owner, persona)** and threads the
> pre-resolved scoped root in at spawn over a dedicated `PERSONA_FILESYSTEM_SCOPE_ROOT` env var.
> No scope ⇒ the child **fails closed** (serve-and-deny) — it never falls back to the shared root.
> The scope is computed server-side from the same source of truth as the in-process tools (so the
> two can never disagree), and bound for the request at loop-build time. The path-traversal guard,
> `O_NOFOLLOW` opener, and structured-error envelope are reused unchanged (one fail-closed contract,
> not two). The stateless builtins (`time`/`calculator`/`weather`) are unaffected — they stay
> process-wide singletons. Off-process behaviour only; no public API or operator-config surface
> changes. Proven with real-subprocess integration tests (cross-owner + single-owner-multi-persona
> isolation, distinct-child-per-scope + reaping, end-to-end serve-and-deny).

### Persona MCP Self-Extension — the setup form (Spec N4 Group D, 2026-06-30)

> The user-facing half of the credential-isolation mechanism, completing N4. A
> schema-driven setup form occupies N3's reserved `needs-setup` slot for an
> **adoptable** remote app (one that declares a credential): the user pastes the
> credential and it posts **straight to the store** via `POST /v1/personas/{id}/adopted-apps`
> — never through a persona turn (N4-D-1). The connection url/auth come from the catalog
> server-side (N4-D-10); the form sends only the secret, clears it on success, and never
> persists it client-side. It sits after the trust disclosure in the expanded card
> (see-then-grant). Local-container / no-credential apps keep the read-honest disclosure +
> the allow-list toggle (the N3 behavior); the form shows only in the edit flow (an existing
> persona to adopt against). A 409 surfaces a clear "already set up" message, not a crash.
### Synthetic-Media Provenance & Disclosure — AI-generated images are recorded and disclosed (2026-06-30)

> Every image the system **generates** is now recorded as generated-vs-uploaded and disclosed as
> AI-generated, per **EU AI Act Article 50** (binding 2026-08-02). The provenance is structural
> and unforgeable — marked at the moment the image is written, scoped to the owning tenant, and
> the user-facing "AI-generated" disclosure is *derived* from the stored signal rather than
> guessed. Both image surfaces are covered: persona **avatars** and **in-conversation** images.

#### Added

- **Avatar provenance.** Personas now carry an `avatar_source` (`generated` / `uploaded` /
  unknown) recorded in the **same write** that sets the avatar — so a generated avatar can never
  be silently passed off as uploaded (or vice-versa), across all four ways an avatar is set
  (auto-generated inline at create, auto-generated by the background job, uploaded at create, and
  uploaded via a later edit). Pre-existing avatars read as **unknown** (an honest default — old
  uploads and old generations cannot be reliably told apart after the fact).
- **AI-generated disclosure on the API.** The persona detail now returns `avatar_source` plus a
  derived `avatar_ai_generated` flag, and the workspace-artifact metadata returns a derived
  `ai_generated` flag — the machine-readable signal a client renders an "AI-generated" badge
  from. In-conversation generated images now carry that provenance on the inline-render payload
  (chat **and** voice), so a generated image shown in a turn is disclosed (it previously carried
  none). The disclosure rides the existing structural signal; it is never a second source of
  truth.

#### Notes

- **No embedded watermark dependency.** Provenance is the durable database/sidecar record, not an
  embedded C2PA/SynthID mark. C2PA is stripped by ordinary reprocessing and cannot prove a
  negative; SynthID is a generation-time, provider-specific embed none of the configured image
  backends expose — so embedding is not technically feasible for this surface, and Article 50
  requires machine-readable marking only "as far as technically feasible." Embedded credentials
  are recorded as a documented future option, not built. This proactively ships an obligation
  that had been deferred (the archetype-not-likeness rationale is outweighed by the near binding
  date), reversing only that deferral.

### Production Safety & Security Hardening (2026-06-30)

> A test-first hardening pass over already-shipped surfaces — no new features. A misconfigured
> cloud deployment now **fails loudly at startup instead of silently shipping authless or
> RLS-bypassing**, credits **cannot be double-spent** under concurrency, the file serve/stage
> layer's symlink-swap (TOCTOU) window is **closed**, and a rotated model-provider key returns
> the **correct HTTP status**. Every fix ships a regression test that fails before it and a
> durable guard (a startup assertion, a DB constraint, or a default-lane test) so the unsafe
> state cannot silently return. The guiding invariant: **fail-fast, never fail-open.**

#### Security

- **Cloud-edition startup fail-fast.** A new guard refuses to start a `cloud` deployment that
  is misconfigured: it requires an explicit `PERSONA_EDITION=cloud`, an `APP_DATABASE_URL` that
  is set **and distinct from** the superuser `DATABASE_URL` (so the request path never runs as
  an RLS-bypassing superuser), and a non-empty `PERSONA_API_JWT_AUDIENCE` (so the JWT `aud`
  check is actually enforced). A defense-in-depth probe additionally refuses if the request
  engine's role is a PostgreSQL superuser. The guard runs at the API composition root **and**
  the background-worker composition root — a misconfigured worker now refuses rather than
  warning. Community (the zero-infra self-host) is unaffected.
- **Credits cannot double-spend.** The credit decrement is now a conditional atomic operation
  (`UPDATE … WHERE balance >= :amount RETURNING`): an overdraw is rejected rather than driving
  the balance negative, and a DB-level `CHECK (balance >= 0)` constraint enforces the floor
  regardless of code path. Post-success turn billing tolerates an exhausted balance gracefully
  (the completed turn is never discarded; the next turn's pre-flight gate still refuses).
- **File serve/stage TOCTOU closed.** A single audited `O_NOFOLLOW` opener now backs every
  sandbox read/write/serve site, so a symlink swapped into a path's final component after
  validation can no longer redirect a read, write, or download outside the sandbox. The
  artifact-delete and image-serve existence checks were also hardened to not follow a trailing
  symlink. (Symlinks in *intermediate* directories remain a documented, accepted v1 residual,
  matching the prior file-tool behavior; the Linux-only `openat2` hardening is noted for later.)
- **Provider-auth status correctness.** A rotated or invalid model-provider API key surfaced
  mid-request now returns `401` with a generic, non-leaking body, instead of falling through to
  a `500`. (The keyless-deployment `503`, shipped earlier, is unchanged.)

### Persona MCP Self-Extension — in-role, credential-isolated app adoption (2026-06-29)

> Close-out of `persona-mcp-self-extension` (Spec N4, backend). A persona that hits an
> **in-role task it has no tool for** no longer dead-ends: it can **discover** a relevant app
> in the mirrored catalog, **propose** it to the user in plain language, and — once the user
> **approves and supplies the setup** — **use** it. Two hard constraints define the shape, and
> both are proven, not asserted: self-extension is **bounded** (in-role + user-approved, never
> silent) and the credential is **isolated** end-to-end (user → encrypted store → MCP; the
> persona's LLM context never contains it).
>
> The load-bearing insight: N4 does **not** build the credential read/inject mechanism — it
> **reuses** the proven Spec-30 bring-your-own path (Fernet-at-rest → transient-decrypt →
> inject at the transport hop), because a shared connect-only gateway structurally cannot vary
> a per-user secret. v1 therefore covers **remote-endpoint apps**; local-container per-user
> secrets are a documented deferral.

#### Added
- **`mcp_search` built-in tool** (`persona-core`) — searches the mirrored catalog by capability,
  returning candidate apps (friendly name, what-it-does, requirement **names only, never a
  secret value**). Keyword-ranked, hard-capped; a semantic-rerank seam is left for later.
- **Gap-detection prompting** — a capability-gated system-prompt block guiding the persona to
  reach for `mcp_search` only on in-role gaps and to **propose, not act**. Safety rests on the
  hard gates (mandatory approval + per-persona allow-list), not the prose.
- **Catalog-app adoption** — `POST /v1/personas/{id}/adopted-apps`: owner-scoped, vetted-gated,
  derives the connection url/auth from the catalog entry (the trust anchor for *where* it
  connects) and takes the credential only via a redacted body field; encrypts it at rest and
  assigns the app to the persona.
- **Vetted-set policy** (`adoption_policy`) — community adopts any remote app; cloud is gated to
  an operator allowlist (`PERSONA_MCP_ADOPT_VETTED`, empty = deny-all, fail-closed), enforced at
  both the grant and `mcp_search` boundaries, scoped to catalog adoption only.
- **Migration `023`** — additive nullable `user_mcp_servers.catalog_source` (adoption provenance).
- **Adversarial credential-isolation proof** — drives a real prompt-injection through the live
  agentic loop with a fully-complying model; a five-channel sweep proves the credential is
  **structurally absent** from the model's context (criterion 3 is impossibility, not refusal).

#### Notes
- **Config:** `PERSONA_MCP_ADOPT_VETTED` (cloud adopt allowlist). Credential encryption reuses the
  existing `MCP_CREDENTIAL_KEY` — no second key.
- **Response shape:** `MCPServerDetail` gains `catalog_source`; a new `AdoptCatalogAppRequest` + the
  adopt route are added (web OpenAPI client regen at merge-back).
- **Deferred:** the OAuth authorization-code handoff and per-user-gateway local-container secrets
  (documented scope cuts); the web setup form follows in a short pass against the regenerated client.

### Skill-injection trust foundation — any skill is safe to inject (2026-06-29)

> Close-out of `skill-injection-trust` (Spec S1). Skills are **prompt content the
> persona follows**, not sandboxed code — and the injector used to splice a
> `SKILL.md` body **verbatim** into the system prompt. Fine for built-in skills; a
> behavioral-hijack vector the instant external sources land. S1 is the
> architectural defense that makes **any** skill content — built-in or untrusted
> external — structurally safe to inject, and it hardens the **live** skill path
> (built-in skills included). It is the security base of the Specialities track:
> S2 (external sources) sets the per-source trust tiers S1 enforces; S3 (frontend)
> renders the tier + collects consent. Framed honestly: skill content is
> **structurally subordinated, tiered, consented, and audited — defense-in-depth,
> not immunity** (no single-stream prompt defense fully defeats a determined
> adversarial `SKILL.md`; the out-of-band architectures that approach provable
> guarantees are out of scope). Proven against a real model: 5/5 adversarial
> attacks ("ignore previous instructions", "reveal your system prompt", "always
> recommend X", role-reversal, forged-delimiter marker-spoof) **resisted**, and a
> legitimate skill still **followed** — both belts, behaviorally. **Inert in
> production today** (every current skill is `builtin` → no consent gate fires);
> it bites only when external skills land. **No migration** (trust + provenance are
> in-memory `SkillSpec` fields). Zero new dependencies.

#### Added
- **The subordination guard** (`persona.skills.guard`) — `subordinate()` wraps
  skill content in a **per-injection nonce-delimited, tier-labelled envelope**
  (the random nonce defeats an adversarial `SKILL.md` forging the closing marker
  to escape and impersonate the system); `SUBORDINATION_PREAMBLE` is the locked
  **scope-don't-suppress** authority framing (grants method/format authority,
  reserves identity / rules / safety / prompt-confidentiality / loyalty);
  `self_framed()` carries both inline for the agentic system-role append; a
  `DEFENSE_CLAIM` constant locks the honest framing (asserted, never "immune").
- **Trust tiers + provenance on every skill** — `SkillTrust` (builtin / vetted /
  community / third_party, with `requires_consent` gating above `vetted`) and a
  `SkillProvenance` model (`source`, `source_uri`, `source_ref`, sha256
  `content_hash` = the re-consent trigger, `signature`) on `SkillSpec`.
  **Source-assigned, never read from author-controlled front-matter** — the
  scanner sets `builtin` + the real sha256 and ignores any self-declared `trust`.
- **Consent gate** — `SkillConsentPort` + a `DenyUnvettedConsent` default-deny
  stub: an above-`vetted` skill is not injected until an explicit consent provider
  (Spec S3) approves it, bound to the skill's `content_hash`. Denied/absent →
  not injected, persona unaffected.
- **Audit** — `AuditAction.SKILL_INJECTED` / `SKILL_REFUSED` + a `"skill"`
  `StoreKind` sentinel; every injection (and every consent refusal — a security
  signal) emits one `AuditEvent` through the existing `AuditLogger`, with
  provenance + consent state in metadata.

#### Changed
- The guard is wired at **all three** skill-content entry points — chat
  `_compose_skill` (depth-1 injector output + composed-verbatim), the prompt
  builder (authority preamble emitted once, below the identity/constraints floor),
  and the agentic `_maybe_inject_skill` system-role append (self-framed, closing
  the hole where skill content could ride a `system` message back to system
  authority). Built-in skills are subordinated too; their activation + injection +
  Spec 24 composition still work unchanged.
- `RuntimeFactory` wires the injection audit sink (the same `JSONLAuditLogger` the
  stores use); the consent gate defaults to deny-unvetted until S3 lands.
### Durable rich chat history — rich turns survive refresh, switch, and reconnect (2026-06-29)

> Close-out of `durable-rich-chat-history`. A live chat turn renders an interleaved
> view — text spans, tool-call cards, tool results, produced files, charts, images
> — but on the read/reconstruct side **only text used to survive**: a refresh, a
> conversation switch, or a mid-stream reconnect flattened a rich turn to plain
> text. This persists nothing new — the assistant turn's **ordered rich event log
> is already captured** durably at terminal (the resumable-turn checkpoint writes
> it to `messages.stream_events`). The fix is **read + reconstruct**: the
> conversation `GET` now returns that log as `events`, and the web rebuilds the
> **identical interleaved view** on initial load and on reload via one shared
> reducer. **No migration** — it reuses the one existing log rather than adding a
> second, parallel source of truth.
>
> Reconstruction folds the persisted log through the **same reducer the live stream
> uses** (`reduceChatEvent`), so the live event union and the persisted shape
> cannot drift — guarded by a two-sided contract test (the frontend live-equivalence
> test + an API-side envelope-shape test pinning every persisted payload against the
> keys the reducer reads). The two previously divergent text-only history maps
> (initial-load builder + `reload()`) are replaced by one `persistedToView` mapper.
>
> One documented graceful degradation: the per-turn **model-decision (`routing`) and
> budget chrome** are not in the persisted log (they ride only the live `done`
> frame), so on reload the tier chip survives but those finer chips do not — the same
> degradation class as legacy text-only rows; the interleaved rich content is
> unaffected.

#### Added
- **`events` on the conversation history response** — `GET /v1/conversations/{id}`
  now returns each message's persisted ordered rich event log (text spans +
  `tool_call`/`tool_result` carrying `kind`, args, result content, and
  artifact/produced-file **refs**), projected from the existing
  `messages.stream_events` checkpoint. `null` on user/tool rows and legacy /
  non-streamed assistant rows → byte-exact text-only render (back-compat).
- **`persistedToView` + a shared `reduceChatEvent` reducer** (web) — reconstructs
  the interleaved view from the persisted log; used on both initial page load and
  reload. The live SSE path folds through the same reducer (one source of truth,
  anti-drift).
- **Two-sided anti-drift contract test** — frontend live-equivalence (persisted log
  → byte-identical view to live frames) + an API-side envelope-shape test pinning
  every persisted RunEvent-dump payload (tool_calling / tool_result+artifacts /
  activity / asking_user / memory_recall) and the text-delta entry against the keys
  the reducer consumes.

#### Changed
- **`reload()` hardening** — it now reconstructs the rich interleaved view (was:
  flattened to text), so the resumable-turn reconcile and post-reconnect recovery
  preserve tool cards / artifacts instead of dropping them (the "rich content
  sometimes disappears by itself" bug). A transient mid-stream disconnect now routes
  through reattach (the detached server turn may still be running) rather than a
  blind reload, and reload preserves an already-rendered rich turn when the persisted
  log is absent (legacy rows).
- Regenerated `openapi.json` + `schema.ts` for the new `events` field.
### Voice capability parity — tools, artifacts, and the preview panel in a call (2026-06-28)

> Close-out of `voice-capability-parity`. A voice call is no longer talk-only: the persona
> can invoke tools mid-call and **produce artifacts that render on screen** in the **same**
> `FileRendererPanel` chat uses, while it narrates by voice. Tools are partitioned by
> measured latency — `web_search`/`render_diagram` run **inline** (the diagram persists its
> source and renders sub-100ms), while `generate_image` (5–20s) runs on a new **async
> production lane**, decoupled from the audio turn: the artifact **renders the instant it's
> ready** (a `tool_result` data-channel frame), and the persona's "it's on screen"
> narration is **floor-gated** — spoken only at the next idle floor via a new
> `LISTENING→PROCESSING` agent-initiated turn, so it never talks over the user (a barge-in
> cancels it). Rich-output rides the **same** `RunEvent` vocabulary chat uses
> (`tool_result`+artifacts / `activity_*` "using X…" badge), over the LiveKit data channel
> instead of SSE — no parallel format, no new dependencies, no migration. Voice dispatch
> routes through the shared activity seam (composes with `persona-activity-events`); the
> async lane is bounded + cancelled at call teardown.
>
> **Also fixed:** the voice tool policy named the dead string `image_generation` while the
> real tool is `generate_image`, so image generation was silently never offered in voice —
> now reachable.
>
> **Known limitations (deferred follow-ups):** in-call tool **consent** beyond
> already-allow-listed tools (the `asking_user` visual confirm) is not yet wired; an
> in-call artifact's **bytes persist** to the workspace but it is **not yet recorded as a
> clickable artifact in the thread/transcript** (only the spoken narration is). Live
> render + audio-timing are validated by a user-run real-browser operator pass.
### Wellbeing-aware layer — care-first handling of sensitive knowledge in the shared graph (2026-06-28)

> Close-out of `wellbeing-layer` (Spec K4). The shared graph deliberately lets every
> persona draw on one accumulated understanding of the user — but one narrow class of
> knowledge (self-harm, disordered eating, acute crisis, abuse, addiction) makes "share
> everything bluntly" a *safety* problem, not a privacy preference. K4 is the system's
> wellbeing principles reaching into the graph: **a floor, not a filter; care, not
> compartmentalisation.** It is **policy over reserved seams** — no schema change, no new
> retrieval mechanics — supplying the one versioned policy artifact that K2 (tag-at-write
> + never-store), K1 (allowlist subtraction), and K3 (the surfacing slot) all consume.
>
> The dominant mode is **share-with-care**: sensitive knowledge still flows to every
> persona, riding **per-category care text** so the persona handles it well (a fitness
> persona that learns of a disordered-eating disclosure receives it *with* a no-precise-
> numbers rule — which is the only configuration in which that persona is safe). Only the
> narrow case where injection itself is the harm is **context-gated**: detailed crisis/
> trauma is subtracted from conversations the user hasn't opened — decided from the
> conversation (query + a short recent window), never persona-type heuristics — and the
> gate **lifts the moment the user raises the topic** (no uncanny re-closing). A tiny
> **write-side never-store** line (with K2) keeps means/method specifics out of the graph
> entirely. The acceptance bar runs both ways: the protective cases hold **and** the
> positive cases keep working (the tutor still adapts, the budget still includes the
> disclosed medication) — over-gating fails the spec as surely as under-protection.
> Migration-free; rides K0's existing `wellbeing_category` column.

#### Added
- **The versioned policy artifact (persona-core):** `persona.wellbeing_policy` — per-category
  retrieval mode (share-with-care vs the narrow context-gated subset = self-harm / acute
  crisis / abuse), the recency-weighted subtraction gate (a stronger gate for older,
  unprompted material), and the never-store boundary. `persona.wellbeing_care_text` — the
  per-category × recency care texts, versioned independently and authored against
  disclaimer-compliance (a warning-then-numbers reply is a harm, not a pass).
- **The runtime providers + the gate:** `persona_runtime.wellbeing` (the surfacing-guidance
  provider for K3's slot, the allowlist provider for K1's subtraction, a lean explainable
  topical-overlap scorer) and `graph_window` (the per-turn recent-conversation window the
  gate reads, carried via a ContextVar set by every conversational loop — chat + voice).
- **Two additive RLS-scoped graph-store reads** (`flagged_nodes`, `node_ids_for_owner`) — no
  schema change (the `wellbeing_category` column already exists).
- **The judged care-effectiveness eval** (three-way rubric: supportive-safe / harmful-
  compliance / over-refusal-first-class) + a build-blocking human felt-quality operator-pass
  charter, and an integration suite proving the gate end to end on the real stack.

#### Changed
- K3's reserved seams are filled (back-compat, defaulted): the surfacing slot now forwards
  recency so care text is recency-weighted; the allowlist seam receives the per-turn gating
  context. Every existing caller is byte-identical until a real provider is wired.

### Discord & Slack — reach your persona on two more chat apps (2026-06-28)

> Close-out of `discord-slack-adapters` (Spec C3) — two **thin** DM adapters on the C1
> framework, following the Telegram (C2) reference. A persona is now reachable in **Discord
> DMs** (over the gateway WebSocket) and **Slack DMs** (socket mode by default; HTTP events
> with request-signing optional). Each reuses the shared inbound flow, the parallel-
> conversation model, identity mapping, and C0-backed outbound — adding only its platform's
> glue. **Two distinctions got their first test:** OAuth-based account linking (Discord +
> Slack) — the first beyond Telegram's deep link — and **DMs as the personal surface** on
> workspace/server platforms (channels/servers are explicitly out of scope). **Zero new
> dependency** (REST over `httpx`, the gateway/socket WebSockets over the already-resolved
> `websockets`); **no migration** (reuses C1's tables). The thin-adapter promise held: the
> framework did the heavy lifting, surfacing only **two additive C1 amendments** (a
> measure-pluggable splitter + a shared inbound-flow skeleton) — folded into C1, not worked
> around in the adapters.

- **Discord connector** (`persona_connectors.discord`) — implements C1's `Connector` + C0's
  `MessageDeliverer`. Inbound `MESSAGE_CREATE` over the **gateway WebSocket** (heartbeat/ACK-
  watchdog/RESUME lifecycle; `DIRECT_MESSAGES` intent only — DM content is exempt from the
  privileged intent); **DM-only** (a server message is ignored); outbound rendered with a
  Markdown **`**bold**`** name tag, split at the 2000-char cap; the typing indicator on slow
  turns. **DM-ability is conditional** (a bot can DM only with a mutual guild / user-install)
  — handled honestly: an un-DMable send → `failed` with a warm "share a server / message me
  first" note, cold C0 origination with no established DM channel → `pending` (durable, never
  lost), never a crash.
- **Slack connector** (`persona_connectors.slack`) — implements the same seams. Inbound
  `message.im` via **socket mode** (default — an app-token-authenticated WS, no public
  endpoint) or **HTTP events** (a signed public endpoint); **`im`-only**; outbound rendered
  with a **mrkdwn `*bold*`** name tag (single asterisk) + `&<>` escaping. Slack DMs are
  **unconditional** (no Discord-style gate). No bot typing indicator in plain DMs.
- **OAuth account linking** (both) — the first OAuth carriers around C1's unchanged linking
  lifecycle: the one-time link token rides as the OAuth `state` (unguessable / single-use /
  short-TTL / platform-bound = free CSRF), the callback exchanges the code → the platform
  identity → binds. A Discord/Slack-linked identity resolves through the shared resolver
  **identically to a Telegram one** (proven), confirming C1's linking abstraction accommodates
  OAuth, not just deep links — with **zero C1 change**.
- **Security:** Slack HTTP-events requests verified by a **constant-time `v0=` HMAC** over the
  **raw body** with a **5-minute replay window**, fail-closed on an unset secret (the per-
  request trust model — the opposite of the gateway/socket connection-auth); Discord gateway
  + Slack socket authenticated by the connection token (no per-message signature); all bot/
  app/OAuth credentials `SecretStr`, never logged; **ownership holds per platform** — a
  Discord/Slack identity reaches only its linked user's personas (C1 RLS, proven cross-tenant
  on real Postgres for both).

### Approvals, permissions & bounds — the autonomy safety spine (2026-06-28)

> Close-out of `approvals-permissions`. Answers what a persona may do **when nobody is
> watching** — and how it asks when the answer is "not without you." The architecture is
> **approval-gated action**: tools declare **action categories** (`observe`/`compute`/`draft`/
> `notify_user` free; `communicate_as_user`/`spend`/`external_mutate`/`credentialed_access`
> gated by default); each task's contract sets per-category policy over conservative defaults; a
> leg hitting a gated action records an **exact proposal**, the task → `waiting(on_user)`, the
> user's natural-language reply routes back, and the task resumes — **approved replays the
> proposal verbatim** (the model never re-derives), denied adapts. Around the gates: per-task
> **budgets** (pause-at-cap + one-reply extend), **kill switches** (task/persona/global),
> **expiry** (remind-once → auto-pause), **cadence caps** (chatter digests; approvals/failures
> always pass), **failure honesty** (no path terminates without a C0 account), and the day-one
> **asks-per-task** gate-fatigue metric. Built on A2's seams (`waiting(on_user)`/resume/CAS);
> the agentic loop is composed **unmodified**. One migration (`approval_proposals` +
> `approval_decisions` + kill-switch/cadence tables + an `audit_log` index); reuses C0/C1 for
> the live channels.

#### Added
- **Action-category taxonomy + per-task policy** (`persona.tools.categories` /
  `category_policy`, `mypy --strict`). Eight categories; the single authoritative
  `resolve_action_categories` mapping (seeded from the tool catalog) with **registration-time
  completeness enforcement** (unmapped / MCP tools default to the gated `external_mutate` — the
  back-door closure); a sparse allow/gate/deny `CategoryPolicy` on the A4 contract. Network-
  enabled `code_execution` escalates to `external_mutate` (bounded egress is still external
  reach).
- **The approval flow** (`persona.approvals` + `persona_api.approvals`). The `ActionProposal`
  (exact verbatim payload) + `ApprovalDecision` records; a model-backed reply interpreter behind
  a deterministic **floor** (`resolve_reply`: approve-only-on-confidence, deny-honoured,
  clarify-once-then-deny, material-modify-re-confirms; NO/EN); the `PolicyGatedToolbox`
  enforcement boundary (allow → run; deny → recoverable result; **gate → record proposal + raise
  → `WAITING_APPROVAL`**, no loop edit); the resolver that **replays the exact payload** into a
  resolution checkpoint with **two-layer at-most-once** (proposal CAS + checkpoint CAS).
- **Bounds.** Per-task budgets over the A2 ledger (effective cap = contract bound + `SUM`
  extensions; pause-at-cap + at-most-once one-reply extend); **kill switches** at three scopes
  with the **reason-scoped runnable invariant** (independent pause sources — clearing one can't
  resume a task another holds); approval **expiry** (remind-once at 24h, auto-pause at 72h,
  leader-gated sweep); priority-classed **cadence caps** (chatter batches to the A6 digest seam;
  approval/failure/safety always deliver); **failure-honesty** accounts (no silent failure,
  exhaustively gated); the **asks-per-task** metric.
- **Migration** `approvals_permissions` (`022`): `approval_proposals` (one-pending
  partial-unique, the replay payload) + `approval_decisions` (composite-FK owner-pinned) +
  `suspended_personas` + `platform_controls` + `cadence_counters`, owner-scoped RLS (operational
  `platform_controls` excepted); an `audit_log(target, action)` index for the budget-extension
  SUM. Budget/extension events ride `audit_log` (no table); the policy matrix rides
  `contract_json` (no column).

#### Notes
- Zero new dependency. The agentic loop / drain / tick / synthesis are untouched (the gate is a
  toolbox wrapper + an executor disposition). No chat-path regression — the chat `Toolbox` is
  unchanged; gating is leg-only.

### MCP-as-apps — the built-in MCP catalog, reframed as "apps" (2026-06-28)

> Close-out of `mcp-as-apps`. Users don't think "MCP servers and tools" — they
> think *"my persona has apps it can use."* This reframes the built-in MCP catalog
> in the web UI from a flat tool-toggle list into a legible **apps experience**: a
> **searchable directory of app cards → per-app detail**, decoupled from the raw
> tools view, with each app's friendly name, description, and trust facts surfaced
> honestly (an app *is* a real integration running real code). Per-persona
> enablement is unchanged — still the `mcp:<name>` entry in the persona's `tools`
> allow-list (now reframed as a see-then-grant toggle inside each app's detail).
> **Pure frontend** over the existing catalog data; no migration, no new backend,
> no new dependency.
>
> Three intentional, decision-backed scope lines (not gaps): the credential
> **form + secret-write** is N4's (the secret-write backend is deliberately
> deferred — connect-only has no per-user secret path); **nested tool NAMES** are
> not shown because the catalog carries no per-server tool list or count at render
> time (verified — tool names exist only at live connect), so an app shows one
> honest capability line, never a fabricated list; **remote `icon_url`** stays a
> deferred opt-in (no `remotePatterns` configured — a local glyph avoids leaking
> the user's IP/referrer to arbitrary hosts).

#### Added
- **Apps chooser** — the MCP catalog as a searchable directory of app cards
  (friendly name + description + a local glyph icon), each expanding to a per-app
  detail. Decoupled from the raw tools view; reuses the existing card / collapsible
  / badge / input primitives (no new component library).
- **Four app states** — `available` / `needs-setup` / `enabled` / `unavailable`,
  from a pure, unit-tested derivation over the catalog entry + the persona's
  `tools` + `unavailable_mcp_servers` (precedence `unavailable > enabled >
  needs-setup > available`).
- **Read-honest needs-setup disclosure** — a credential-declaring app shows what
  it needs (from the catalog's display-only credential schema) as **informational
  text** ("needs `GITHUB_TOKEN` — configured at the deployment level"), naming who
  sets it; never a form, never a disabled field, never "your credential is missing"
  (the web has no credential read-back).
- **Trust disclosure (legible-not-opaque)** — a compact signal on the card
  (signed mark + coarse risk) and full provenance in the detail (image / source
  project + commit / egress allow-hosts), so a user can see an app is real code
  reaching real hosts before granting it.
- **Unavailable-app tombstones** — on the persona detail page, apps the persona
  enabled that were removed from the catalog (`unavailable_mcp_servers`) render as
  graceful, informational tombstones ("removed from the catalog — the persona
  keeps running"); no re-add action (the server is gone).
- **`apps.*` i18n namespace** — the canonical "apps" copy; no raw "MCP server"
  jargon in the primary surface.

#### Changed
- The persona form's MCP section is now the **apps chooser** (reframed presentation
  of the former flat `McpToggle` chip row); the underlying `mcp:<name>` tools-list
  enablement mechanism is unchanged. The enable toggle lives in each app's detail
  panel — an **intentional see-then-grant flow** (you see an app's provenance before
  granting it real-code capability), not a limitation.
- Regenerated the OpenAPI web client to surface the catalog's already-shipped N1
  display/trust/credential-schema fields + `PersonaDetail.unavailable_mcp_servers`.

### Persona activity events — live "using X…" states + traceability (2026-06-28)

> Close-out of `persona-activity-events`. Emits a structured **activity-start** event the
> moment the persona is about to use *any* capability — builtin tool, skill, MCP tool,
> sandbox/code-execution, image generation, web search/fetch, memory recall — paired with
> an **activity-end** on completion. The chat + run views render a live "Searching the
> web… / Running code… / Creating an image…" state (closing the v1 "is it stuck during a
> long tool turn?" gap), and every turn/run leaves an ordered, persisted trace.
> Generalizes Spec 35's named-store recall moment to all capabilities, through **one**
> typed `RunEvent` extension emitted at a **single dispatch boundary**. Purely additive
> instrumentation — no behaviour change. **Zero new dependency; no migration.**

#### Added
- **One emit seam = total coverage.** A core-defined `ActivityObserver` port + an
  `ObservedToolbox` decorator wrap the single `toolbox.dispatch` boundary (outermost, so
  it composes with the in-flight approvals gate: activity emit outer, gate inner). Every
  capability the persona invokes mid-turn — chat, runs, and (forward) voice — emits without
  a per-surface event layer. A structural test asserts the runtime dispatches tools in
  exactly one place, so a new capability cannot ship silent by omission.
- **Unified `activity_start` / `activity_end` `RunEvent` kinds** (kind / label / name /
  status / `activity_id` / timestamps), carried over both SSE transports and persisted via
  the existing event-log JSONB (chat `stream_events`; run `runs.steps` while running).
  Coexists with `tool_result` / `memory_recall` (kept emitting); consumers render the live
  state from the activity contract, the tool card from `tool_result` — one per call, no
  double-render. `memory_recall` now also emits for agentic runs (parity with chat).
- **Redaction at the emit boundary** — a key-denylist + nested redaction + value
  truncation + total cap on the args summary, so no credential / API key / sensitive input
  leaks into an activity event.
- **Live "using X…" UI** — an identity-tinted, reduced-motion, ARIA-announced
  (collapse-to-current) affordance in the chat message + run step card, generalizing the
  Spec 35 named-store recall component to every capability.

### Call history & transcripts — a browsable home for voice calls (2026-06-27)

> Close-out of `call-history-transcripts` (Spec V9). A finished voice call now
> leaves a navigable, re-readable trace instead of vanishing. A single **immutable
> `origin` marker** on the conversation (`chat` | `call`, set once at creation) is
> the ONLY seam between chat and voice: the chat list excludes call-born
> conversations (no more empty "Untitled conversation" pollution), the Calls
> surface shows them. The spoken turns now **persist as real conversation
> messages** — byte-for-byte with a chat turn — so a call's transcript renders
> under the same thread UI, and a durable **call-record** (persona / time /
> duration / end reason) powers a **Calls sidebar section** + a **`/calls` history
> page**. The voice runtime stays **API-free**: it writes through core-owned table
> views on its own RLS engine (the `memory_chunks` precedent), never importing
> persona-api. Transcript-only (no audio recording). Two additive migrations
> (`conversations.origin`, `calls`); zero new dependencies.

#### Added
- **`conversations.origin` marker** (`chat` | `call`) — the immutable birth-marker,
  the single seam between chat and voice. The chat conversations list excludes
  `origin=call`; a call-born conversation surfaces in the Calls surface instead
  (acceptance #1). Set once at creation by the web; backfilled to `chat` for every
  pre-V9 conversation.
- **Durable call-records** (`calls` table, RLS owner-scoped) — each voice call's
  envelope (persona / `started_at` / `ended_at` / `duration` / `end_reason`),
  written by the voice runtime at call teardown. `GET /v1/calls` lists them
  newest-first, paginated, owner-scoped; each row carries the `conversation_id`
  that links to its transcript.
- **Saved transcripts** — a call's spoken turns (user STT + persona heard text)
  persist as conversation `messages`, speaker-attributed + timestamped,
  byte-for-byte with a chat turn, so the transcript renders identically under
  `GET /v1/conversations/{id}` and the existing chat thread (no separate renderer).
- **Calls web surface** — a "Calls" section in the sidebar (recent calls), a
  `/calls` history page (full list), and a primary-nav entry; every row opens the
  call's saved transcript.

#### Changed
- The chat conversations list now **excludes call-born conversations**, filtered
  solely on the `origin` marker (no inspection of voice/call state — the only-seam
  discipline).
- The web call-create paths (the persona-card "Call" + the `startVoice` action)
  mark new conversations `origin=call`; text-create paths mark `chat`.
- The V7 post-call recap note now sits **above the rendered transcript** in the
  thread — V9 made the spoken turns durable, so the thread itself is the transcript.

### Graph-aware prompts — the shared brain enters the persona's behaviour (2026-06-26)

> Close-out of `graph-aware-prompts` (Spec K3). Where the shared knowledge graph
> stops being a database and becomes *felt*: every persona now draws on what the
> user told **other** personas — the tutor adapts to the focus struggles mentioned
> to someone else; the planner budgets for the move it was never told about
> directly. The graph is an **additive, independent** retrieval source alongside
> the persona's own memory (both queried per turn, no precedence, no conflict
> logic). The genuinely-hard part is **usage, not plumbing**: knowledge learned
> through another persona is used *naturally* — applied where relevant, never
> recited, never paraded, tentative when old, honestly attributable when you ask
> "how do you know that?". Zero new dependencies; migration-free (it reads K2's
> graph). Identical to before for any user whose graph is empty.

#### Added
- **Graph-knowledge in the prompt** — a per-turn block of what-is-known-about-you,
  rendered in the supplementary region (below the identity/constraints floor,
  beside the persona's own retrieved memory). Each item carries a light recency +
  source note — enough to frame old knowledge tentatively and to answer "how do
  you know?" honestly, never a metadata dump. A **versioned usage-guidance
  artifact** rides with the block, encoding natural use (no narration of the
  mechanism, no gratuitous display).
- **Relevance-thresholded injection** — the graph is queried every turn (cheap),
  but a node is injected only when it genuinely bears on the turn: a substantive
  turn pulls what's relevant, small talk pulls nothing. The threshold gates on the
  real signal — dense semantic similarity — **validated against representative
  turns** (not inherited), so the graph never stuffs the prompt.
- **Graph-aware on every channel** — the extension lives in the shared prompt
  builder, so web chat, voice, and connectors are graph-aware identically. Voice
  fits a tighter, latency-safe profile that **never adds serial wall-clock** to
  the spoken-turn response.
- **Wellbeing safety at the prompt layer** — sensitive knowledge a persona should
  not surface is removed before retrieval AND re-checked at the last surface
  before the model (dual enforcement); a reserved slot lets category-specific care
  guidance ride alongside any sensitive knowledge that is surfaced.

#### Changed
- **Graph knowledge is a budgeted consumer** of the context window — under
  pressure it sheds gracefully (fewer nodes, then none) before the persona's own
  core memory yields, with identity and constraints always held as the floor.
  A user with no graph gets a byte-identical prompt — the graph is additive
  presence, invisible until there is knowledge.

### Docker MCP catalog auto-sync (2026-06-27)

> Close-out of `mcp-catalog-auto-sync`. Keeps the mirrored Docker MCP catalog **fresh
> automatically** so new MCPs become *available* without hand-editing. A leader-gated
> periodic task in the worker loop re-pulls `github.com/docker/mcp-registry` on a
> daily-ish cadence and reconciles the mirror — **adds** new servers, **updates** changed
> ones, **marks removed** ones — reusing the offline pull from the Gateway-mirror spec.
> The hard line: the sync updates **availability**, it **never auto-enables** — enabling a
> market MCP stays the deliberate per-persona allow-list choice (especially in cloud).
> Reuses the durable-job / scheduling substrate (no second scheduler, no system-owner
> sentinel). **Zero new dependency; no migration.**

#### Added
- **Automatic catalog refresh.** The worker loop runs a third ownerless periodic task
  (alongside maintenance + the scheduler tick): `_maybe_run_catalog_sync` fires on a
  daily-ish cadence (`PERSONA_MCP_SYNC_INTERVAL_SECONDS`, default 86400), leader-gated by
  its own Postgres advisory lock so exactly one process pulls. The first sync fires shortly
  after boot (a fresh mirror on deploy), then daily.
- **Reconcile with change counts.** A new offline reconcile (`mirror_reconcile.py`) diffs the
  freshly-pulled entries against the existing snapshot over frozen-model value-equality and
  writes atomically — reporting **added / updated / removed** (logged each run: ran-at +
  counts). Re-running against an unchanged registry is a provable no-op (all-zero diff +
  byte-identical file). Reuses the shared pull seam (`build_entries_from_source`) so the
  git clone is not reinvented.
- **Writable, reader-visible mirror location** (`PERSONA_MCP_MIRROR_PATH`). The request-path
  loader prefers the auto-synced override (the mirror on the mounted volume) over the bundled
  snapshot, falling through to the built-in catalog — every step fail-soft. (Single-machine
  in-process deploy: the sync writer and the catalog readers are the same process on the same
  volume, so a file mirror is reader-visible with no migration.)
- **Opt-out** (`PERSONA_MCP_SYNC_ENABLED=false`) for local/community deployments that don't
  want a periodic outbound git clone — availability then stays at the bundled snapshot.
- **Graceful removed-server handling + an owner-visible signal.** A removed catalog server is
  dropped from availability (never offered as newly-enableable) and the live tool-call path
  already degrades without crashing; a persona that still enables a now-removed server is
  surfaced via the additive `PersonaDetail.unavailable_mcp_servers` field (the disappearance
  is flagged, not silent).

#### Security
- **Availability ≠ enablement (no auto-enable).** The sync's entire write surface is the
  mirror snapshot — it has no handle to any persona's allow-list, so a newly-available server
  is never auto-enabled, never default-on, and never gained by a persona that didn't
  explicitly enable it. Enabling stays the per-persona gate (and the operator-vetted gateway
  boundary in cloud).

### Docker MCP Gateway integration & catalog mirror (2026-06-25)

> Close-out of `docker-mcp-gateway`. Turns MCP from "configure each server, per
> user" into "**enable once in Docker, then opt each persona in.**" The runtime
> connects to an externally-run **Docker MCP Gateway** as a single aggregating MCP
> endpoint, and the apps catalog is a **mirror** of `github.com/docker/mcp-registry`
> (~300 servers), synced offline. Local-first and **connect-only** (Persona never
> spawns the gateway — no Docker socket), reusing the existing streamable-HTTP + bearer
> client path → near-zero new transport code. **Credentials never enter a model turn.**
> Zero new dependency; **no migration**.

#### Added
- **Docker MCP Gateway as a 4th MCP source.** Set `PERSONA_DOCKER_MCP_GATEWAY_URL`
  (the gateway's `--transport streaming` `/mcp` endpoint) and every enabled server's
  tools arrive through one connection, prefixed `mcp:docker:<tool>`. Connect-only;
  operator-trust (not SSRF-pinned, like the existing operator MCP channel); optional
  bearer (`PERSONA_DOCKER_MCP_GATEWAY_TOKEN`, a secret — header-only, never logged).
  **The per-persona allow-list stays the gate** — a server enabled in Docker is not
  auto-granted; each persona opts in by listing the tool (an un-opted persona never
  even connects the gateway).
- **Catalog mirror.** An offline sync clones `docker/mcp-registry`, parses each
  `server.yaml`, and writes a local `mirror.json` the catalog loads **zero-network** at
  request time (verified: 328 live entries parse under the strict model). The
  `/v1/mcp-catalog` surface gains additive display metadata — friendly name, icon,
  image, provenance commit, signing/trust labels, and each server's **credential
  schema** (which secrets it needs — *never a value*, by construction). The built-in
  catalog is the floor (builtin-wins on a name collision); a missing/corrupt mirror
  **fails soft** to the built-in catalog (boot never breaks).
- **Edition split (honest).** Community = the full local gateway integration. Cloud =
  the mirror shown + connect-only to an operator-**vetted** gateway; it **refuses to
  start** with a gateway URL unless the operator acknowledges the shared-across-tenants
  posture (`PERSONA_ALLOW_CLOUD_GATEWAY=1`). Per-tenant gateways, container-running, and
  per-user secret injection are deferred.

#### Security
- **Credential isolation, proven end-to-end.** The gateway bearer flows operator →
  transport → gateway and reaches **no model-facing surface** — not the prompt's tool
  specs, tool-call args, tool result, or audit log (a real-dispatch adversarial test
  confirms it reaches the wire yet appears in none of them). The mirror's secret schema
  is display-only. Per-user secret injection is a defined-but-reserved seam for a future
  release (no live path connect-only can deliver).

### The autonomous task model (2026-06-25)

- The **task** — a durable entity *above* runs. Spec 06's agentic loop executes minutes
  of work in one session; a **task** spans hours or days through **many** such sessions
  (**legs**), *waits* between them (on time, on the user, on the world), and **resumes as a
  coherent continuation, not an amnesiac restart**. A leg is one agentic run executed as one
  durable job, **boxed** (steps + per-leg budget + a wall-clock bound inside the worker's
  drain) so any disruption — a deploy, a wait, a cancel — degrades to the same cheap
  operation: *finish the box, write the checkpoint, stop.*
- The thing that carries between legs is the **checkpoint** — durable working state recording
  *progress (conclusions), intent (plan + next step), pointers (workspace artifacts), open
  questions* — **never transcripts**. It is size-bounded (a 2000-token budget), so a long task
  reflect-and-compacts rather than bloating. Every leg reconstructs context in a fixed order —
  **contract → checkpoint → last-N leg summaries → live retrieval → the leg's trigger** — so it
  holds yesterday's conclusions against today's knowledge.
- `persona-core` `persona.tasks`: the frozen `TaskCheckpoint` + size-bound; the `Task` entity +
  state machine (`defined → active → waiting(until_time|on_user|on_event) → … → completed |
  failed | cancelled`, `paused` overlay) + the cost ledger + the monotonic checkpoint-sequence
  idempotency anchor; the `Contract`; the pure context-reconstruction ordering; the leg box; the
  resume-trigger seam; and the outcome reports (completion / stuck / cancellation — distinct
  types, so a failure is never rendered as a success).
- `persona-runtime` `persona_runtime.legs`: the leg executor (composes the **unmodified**
  agentic loop, enforces the box at a step boundary, writes the checkpoint), milestone-grained
  episodic distillation (no per-leg memory spam), and the production checkpoint distiller.
- `persona-api` `persona_api.tasks`: the RLS-scoped, audited task + checkpoint stores (the
  atomic compare-and-set append — at-least-once becomes effectively-once at the task layer); the
  task-leg job handler (a leg as a durable job, additive in the worker); self-continuation,
  `waiting(on_user)` + resume, completion, failure-after-retries → `waiting(on_user)` with an
  honest stuck-report (from the durable dead-letter queue), and cancellation.
- The **continuation evaluation** — a judged scenario suite (research / monitoring / preparation
  missions, each with a planted established-conclusion and an injected fresh fact that
  invalidates a standing plan-step) scored coherent-continuation vs amnesia vs ossification, the
  direction's core quality evidence.
- New migration: `tasks` + `task_checkpoints` (RLS-scoped; the `UNIQUE(task_id, checkpoint_seq)`
  compare-and-set anchor). No new dependencies. The agentic loop is unmodified.

### Community edition hardening — boots keyless gracefully (2026-06-25)

> The community edition (SQLite + Chroma, no Clerk, no required model key) assumed
> cloud configuration in two places that 500'd a keyless boot. The fix reads model
> capability as the static fact it is, and turns "no model configured" into a clean
> 503 instead of a leaked 500 — plus a durable HTTP smoke suite so a cloud-assuming
> change fails loudly in CI instead of silently breaking the self-host path. Cloud is
> byte-unchanged. Zero new dependency, no migration.

#### Fixed
- **Persona detail renders with no model key.** Capability hydration
  (`GET /v1/personas/{id}`, list, and create's returned detail) no longer
  instantiates a live model backend just to read whether a model supports vision —
  it resolves the answer statically from `(provider, model)`, the same lookup the
  backend itself uses. A keyless community boot used to 500 here; it now returns the
  correct capability (vision `true`/`false`) without a key.

#### Added
- **`503 model_unavailable` on the model-required write paths.** Persona authoring,
  chat turns, and agentic runs now return a clean `503` ("model backend is not
  configured", with `Retry-After`) when the deployment has no usable model backend,
  instead of leaking a `500`. Route-local by design — a configured (cloud) deployment
  is unaffected, and a cloud bad-key still surfaces through its normal path.
- **Community HTTP smoke suite, in CI.** A no-cloud-config (SQLite + Chroma, no model
  key) suite drives create → get → list (capability hydration) and a chat turn through
  the real HTTP surface, plus the keyless `503` — run in the default test job so a
  cloud-assuming regression fails loudly instead of silently breaking the self-host path.

### Telegram — reach your persona on Telegram (2026-06-25)

> Close-out of `telegram-adapter` — the **first concrete connector**, co-developed with the framework to prove it. A persona is now reachable on **Telegram**: message the bot by name and the shared flow drives a reply; link your Telegram account from the web with a one-time deep link; switch personas, `/new`, idle boundaries all work over a real chat. Deliberately **thin** — Telegram's protocol surface only; everything else (routing, persona selection, the conversation model, identity mapping, C0 delivery) is the framework's, *used* not reimplemented. **Zero new dependency** (the Bot API over `httpx`; webhook over FastAPI/uvicorn — all already in the lock).

#### Added
- **Telegram connector** (`persona_connectors.telegram`) — implements C1's `Connector` + C0's `MessageDeliverer` for Telegram. Inbound Telegram updates → C1's normalised shape (tz-aware UTC, `from.id`→identity, `chat.id`→channel); outbound persona replies rendered with an **HTML bold name tag** and split on natural boundaries at Telegram's 4096-char cap (UTF-16-aware, never mid-word/mid-surrogate); non-text (voice/photo/sticker) declined gracefully; a "typing…" working indicator on slow turns. **Account linking** via the `t.me/<bot>?start=<token>` deep link (single-use, short-TTL, opaque token — reusing the framework's bind, adversarially tested). **The runnable service** (`python -m persona_connectors`): long-poll (dev) or webhook (prod) transport, the authenticated linking issue route, and the periodic idle sweep.
- **Security:** webhook updates validated by a constant-time secret-token check **before** parsing, fail-closed on an unset secret (mandatory); the bot token a `SecretStr`, never logged; **ownership over the platform holds exactly as on the web** — a Telegram identity reaches only its linked user's personas (C1 identity-mapping + RLS, proven cross-tenant on real Postgres).
- **Framework co-development (criterion 10):** five small additive C1 corrections surfaced + made *in the framework* (not worked around in the adapter), so C3–C5 inherit them — the `conversation_id→channel` and active-persona reverse reads, the `apply_new` port-completion, the platform-agnostic routing decision (`decide_route`), and an idle-timer fix so an actively-used chat is never swept mid-conversation.

### Graph write paths — every interaction extends the shared memory (2026-06-25)

> Close-out of `graph-write-paths` (Spec K2). How the shared knowledge graph (K0)
> gets **filled**: two paths converging on K0's one merge. A persona can record an
> explicit, durable fact about you mid-conversation (a silent note), and a
> background **synthesis** pass distils the *emergent* understanding a completed
> conversation or agentic run carried but never stated as a single fact — both
> off the critical path, so the reply is never blocked. The heart is **grounded
> extraction**: only what you actually conveyed enters the graph — never a
> speculated diagnosis, an inferred cause, or self-harm method/means. Measured and
> gated on the wired model tier (hallucination ≈ 0). Zero new dependency; one
> additive migration (`synthesis_markers`).

#### Added
- **`record_user_fact` — a direct-write tool**, on by default for every persona:
  the persona records a discrete, durable fact in your own words via ONE fast inline
  graph write (no model call — fire-and-forget, never slows the reply). Grounded by
  construction (`source=persona_self`), owner-scoped (RLS).
- **Off-critical-path synthesis** — a durable, idempotent background job (A0's second
  tenant) runs at interaction boundaries (web turn-end, completed agentic run — Spec 06
  reflection metadata) and distils durable knowledge into the graph with provenance.
  Long interactions are windowed (compacted summary as context, the verbatim tail as
  the only grounding source — a summary never becomes a fact).
- **Grounded-extraction safety, measured + gated** — a committed labelled corpus (EN +
  Norwegian Bokmål) + deterministic metrics; the build-failing gates (hallucinated-
  knowledge ≈ 0, self-harm-means in zero candidates, speculative causation declined,
  sensitive-disclosure tagging) are **green on the wired synthesis tier**.
- **Self-harm means backstop** — a deterministic, non-blocking guard that rejects
  method/means specifics on the direct path before the write (and as defense-in-depth
  in synthesis), so the graph never stores them — only the care-relevant struggle.
- **Wellbeing-category tagging at write** — sensitive disclosures carry K4's category
  tag the moment they are written (the shared `WellbeingCategory` vocabulary).
- **Entity resolution + typed links** — alias-heavy mentions ("my doctor… Dr. Hansen…
  she…") resolve to one canonical entity; temporal/causal links are asserted only on
  the user's stated ordering/causation.

#### Notes
- The synthesis cost is metered + attributable per job (`job.spend`, `surface=synthesis`);
  the precise per-job micros figure is a follow-up refinement.
- Voice-conversation synthesis is a one-line follow-up once the voice service composes a
  job queue; the web + agentic boundaries are live.

### Persistent & resumable sessions — turns + runs survive navigation (2026-06-24)

> Close-out of `persistent-resumable-sessions`. A chat turn or an agentic run keeps
> running server-side when you navigate away, reload, or close the tab; it persists
> its progress incrementally and, on return, is re-fetched and re-rendered — resuming
> the live tail if it is still running. **The persona never stops mid-work just because
> you left.** The chat/run sibling of the persistent-voice experience. Zero new
> dependency; one additive migration.

#### Added
- **Detached chat turns** — a chat turn now runs as a background task (a
  `ChatTurnRegistry`/`ChatTurnHandle` mirroring the agentic-run worker), so a client
  disconnect (navigate / reload / tab-close) **no longer cancels it**. Exactly **one
  active turn per conversation** — a second send while one runs is **blocked** (HTTP
  409), backstopped by a DB partial-unique index.
- **Incremental checkpoint persistence** — the user message + an in-progress assistant
  row are persisted at turn START, then checkpointed (throttled — never per-token) as
  the turn streams, so a reload never shows less than what happened.
- **Chat reattach surface** — `GET /v1/conversations/{id}/active-turn` (the seed),
  `GET …/active-turn/events` (resubscribe to the live tail — the same SSE generator the
  POST streams, never a fork), and `POST …/active-turn/cancel`. The web chat reattaches
  on return (seed-then-tail + reconcile-on-end) and aborts the fetch — not the server
  turn — on navigate-away.
- **Run reattach hardened** — `runs.steps` confirmed as the durable floor (the full
  event-log is persisted per event); the run view re-fetches + resumes the live tail on
  return, showing steps that occurred while away.
- **A startup restart sweep** reconciles any chat turn / run left in-flight by a process
  restart to a terminal state (chat → `interrupted`, run → `error`) — the honest
  single-worker cross-restart story.
- **"Active work" indicators** — a subtle "working" pulse on the conversation row plus a
  global "return to it" bar (the additive chat/run sibling of the persistent-call
  mini-bar), advertising the resumable work; a poll clears the indicator when a turn
  finishes while you are away.

#### Changed
- **Honest billing (revises the credits-on-disconnect rule)** — a turn that **completes
  while you are away is billed** (it ran); an **explicit cancel, an error, or a
  restart-interrupt is not** (no partial billing). The deduct fires on the detached
  completion path, not the request connection; the pre-flight 402 stays at the route top.

#### Fixed
- **Community (SQLite) second-chat-turn 500** — SQLite datetimes now read back UTC-aware,
  fixing a tz-naive `created_at` collision with the persist-at-start path (found during the
  community operator pass).

### Connector framework — the trunk for messaging-platform reach (2026-06-24)

> Close-out of `connector-framework` (the new `persona-connectors` package, the 5th
> uv workspace member). The shared framework that makes a persona reachable on
> messaging platforms — the trunk all per-platform adapters (Telegram/Discord/Slack/
> WhatsApp/SMS/email) plug into. Product model unchanged: **my persona, reachable by
> me** — an authenticated extension of the user's own account, isolated exactly as on
> the web (Spec 08 ownership/RLS). **Framework-complete**; a persona becomes reachable
> on a platform when the first adapter (Telegram) lands. Zero new dependency.

#### Added
- **`persona-connectors`** — a separate long-lived process (the 3rd, after api +
  voice) that reuses persona-api's reply-producing chat flow + the C0 delivery
  boundary in-process (the `run_worker.py` pattern), under the `current_user_id`
  RLS contextvar. The **owned surface** (`persona_connectors.domain`) is
  import-decoupled from persona-api — enforced by an executable AST guard — so the
  framework contracts can extract to persona-core later without a reshape.
- **The `Connector` protocol + normalisation contracts** (`domain`) — a provider-
  independent `NormalisedInbound` (the six-platform-intersection core + optional
  capabilities + a `raw` passthrough), a semantic outbound identity tag (reusing
  C0's `PersonaIdentityTag`), and a per-connector `Capabilities` descriptor +
  render-tier ladder. Designed to the email/SMS floor so all six platforms fit; each
  adapter (C2–C5) is thin.
- **The identity-mapping security spine** — a one-time link token (sha256-at-rest)
  binds a platform identity to a Persona user; thereafter every inbound resolves
  through a live binding and Spec 08 RLS scopes the rest. Adversarially tested:
  cross-tenant access impossible, a partial-active UNIQUE blocks identity hijack,
  an unlinked identity gets a link-instruction and zero access.
- **The per-persona parallel-conversation model** — each persona keeps its own
  conversation per user per channel; naming a persona foregrounds it and *suspends*
  (never ends) the previously-active one, so in-flight work survives switching;
  re-naming the active persona is a no-op. The atomic `SELECT … FOR UPDATE` flip
  serialises concurrent switches. `/new` and the idle-timeout are the only operations
  that end a conversation, per-persona-per-channel.
- **Name-parsing / persona addressing** — sticky-active-pointer-first, precision-over-
  recall: leading/trailing-position only, a vocative comma required for trailing,
  exact Unicode-aware whole-word matching (stdlib `re`, no new dependency).
- **Two additive connector migrations** (`connector_link_tokens` + `connector_identities`;
  `connector_channels` + `connector_conversations`), all owner-scoped under RLS, with
  **no column added to `conversations`** (an additive FK only). The idle sweep is
  served by a `(status, last_activity_at)` index.

#### Not yet (the co-developed Telegram-adapter leg)
- The end-to-end inbound→reply flow wiring, the concrete outbound `MessageDeliverer`
  delivering name-tagged messages through C0, the web-app linking-handshake endpoint,
  and the live-platform integration land with the first adapter (Telegram) — the
  framework ships every shared piece they compose.

### Scheduling — the clock (2026-06-23)

- A durable, RLS-scoped **schedule** entity (RRULE-class recurrence — daily/weekly/
  monthly/yearly, intervals, `BYDAY`/`BYHOUR`/`BYMINUTE`, "first Monday", `COUNT`/
  `UNTIL` — **or** a one-time future instant) with the **user's IANA timezone
  captured on the row**, plus a single-leader **scheduler tick** hosted in the
  worker. "Every morning at 7" means the user's 7, reliably, across DST and travel.
- `persona-core` `persona.schedules`: the frozen `Schedule` entity + `RecurrenceRule`
  (round-trips to/from an RFC-5545 `RRULE` string), and the pure, **DST-correct**
  `next_fire_after` — the spring-forward gap fires at the adjusted instant, the
  fall-back fold fires once (the first occurrence); the missed-fire policy decision
  (`decide_fire`); and the `schedule_id + fire_time` idempotency-key + handoff-payload
  contract. Exhaustive DST fixture suite (both transitions × both edges, a
  reversed-DST southern-hemisphere zone, `COUNT`/`UNTIL` exhaustion).
- `persona-api` `persona_api.schedules`: the RLS-scoped, audited `ScheduleStore`
  (CRUD + pause/resume/edit + record-fire + one-time completion; an edit recomputes
  next-fire but preserves the recurrence anchor + fire count — no COUNT-reset
  loophole), single-leader election via a **session-scoped Postgres advisory lock**
  on a dedicated connection, and the **scheduler tick** that claims due schedules
  cross-tenant and materialises each into an A0 job owner-scoped, keyed by
  `schedule_id + fire_time` (effectively-once — a double tick / leader handover /
  crash-rerun fire exactly once). Missed-fire policy: `fire-late-once` (catch up
  once within a kind-relative grace window) or `skip-and-note`, with a durable miss
  note — **never burst-replays** a backlog. Each fired job carries the schedule
  identity + fire time so a downstream task leg can anchor on it.
- The tick rides the existing worker loop **additively** (an optional, leader-gated
  step — a worker without it behaves exactly as before; zero runtime coupling).
- New `schedules` table (migration `013`), under RLS, with a partial due-claim
  index. New env vars: `PERSONA_SCHEDULER_*` (tick interval, batch size, grace
  windows, on-time tolerance). One new dependency: `python-dateutil` (the RFC-5545
  recurrence engine; zero new transitive surface). No new infrastructure.

### Durable execution & the job system (2026-06-22)

- A Postgres-backed job queue (`SELECT … FOR UPDATE SKIP LOCKED`, claim-then-commit)
  and a new **worker service** (the fourth process class) for background/unattended
  work that must survive a restart, run while nobody is connected, and resume after
  a crash. Replaces the in-process `asyncio.Task` substrate for background work; the
  live chat path is untouched (additive infrastructure).
- `persona-core` `persona.jobs`: the job model + `queued→claimed→running→
  succeeded|failed|dead` state machine, frozen-Pydantic payloads, lease/retry
  policies, and a Toolbox-style typed handler registry (`JobRegistry` / `JobContext`
  / `JobHandler`).
- `persona-api` `persona_api.jobs`: the durable queue (lease + heartbeat
  crash-resume, retry with capped-exponential backoff + jitter, dead-letter with
  cause, claim-time per-user/global fairness caps, terminal-job archival +
  retention) and the worker (composition root, continuous loop, graceful drain,
  maintenance sweep, health probes). Honest **at-least-once** delivery — every
  handler is idempotent by contract, proven by forced re-delivery tests.
- **Avatar generation** is the first durable tenant — idempotent (skip-if-set +
  compare-and-set), enqueued from persona-create behind `PERSONA_API_AVATAR_VIA_QUEUE`
  (default off; the create contract is preserved until the worker is deployed).
- New `jobs` + `jobs_archive` tables (migration `011`), under RLS, with claim-tuned
  partial indexes and per-table autovacuum tuning. New env vars: `WORKER_*`,
  `WORKER_DISPATCH_DATABASE_URL`, `PERSONA_API_AVATAR_VIA_QUEUE`. No new runtime
  dependencies (built in-house on SQLAlchemy Core + sync psycopg3).

### Hybrid retrieval over the knowledge graph (2026-06-22)

> Close-out of `hybrid-retrieval` (persona-core). The retrieval layer that makes
> the K0 knowledge graph usable: dense (semantic) and sparse (lexical/BM25)
> retrieval fused so precise facts about a person are findable — dense for meaning
> ("prefers worked examples" without the word "learning"), sparse for exact terms
> ("metformin" decisively). **Pure orchestration over K0's landed read contract —
> zero new dependency, no K0 fork, no re-rerank.**

#### Added
- **`HybridRetriever`** (`persona.graph.retrieval`) — `retrieve(owner_id, query,
  *, allowlist=None, top_k=None) -> list[HybridResult]`. Runs K0's dense
  (already exact-reranked) and sparse (Postgres FTS) legs independently over the
  same scope, fuses via weighted **RRF** (parallel, **never gated** — a
  paraphrase-only match survives fusion), expands one bounded **type-aware** hop
  along the typed links (ENTITY > CAUSAL ≈ TEMPORAL > SEMANTIC, augment-never-
  displace), and returns hybrid-ranked nodes within a result budget.
- **`reciprocal_rank_fusion`** + **`HybridResult`** (`persona.graph.fusion`) —
  rank-based fusion (`Σ_leg weight·1/(rrf_k+rank)`, k=60, no score
  normalization) and the frozen K3-facing result shape (fused rank + per-leg
  `dense_rank`/`sparse_rank` provenance + node), which makes the no-gating
  property observable.
- **The wellbeing (K4) allowlist seam** — user-scope isolation stays in K0 (RLS +
  in-kernel dense allowlist); the K4 *subtraction* (`user_nodes − flagged`) is
  enforced as a **post-fusion filter over all legs** (isolation/safety, not
  relevance — no-gating preserved), closing the sparse-leg gap (`search_fts` has
  no allowlist param) without re-opening K0.
- **Additive `GraphSettings`** (`PERSONA_GRAPH_*`) — `rrf_k`, `dense_weight`,
  `sparse_weight`, `result_budget`, `dense_pool`, `sparse_pool`, and the
  traversal knobs (`traversal_seed_count`, `traversal_per_node`,
  `traversal_budget`, per-link-type weights). A both-weights-zero config is
  rejected.

#### Notes
- No model-callable tool surface — **operator pass exempt** (pure-library spec).
- Tuning defaults are **measured, not asserted** by an `@external` full-stack
  scale test (dense+rerank + FTS + RRF + traversal + K4 filter over a ~1800-node
  multi-user graph): latency p95 within a per-turn budget, dense recall@10 vs a
  float32 baseline, and confirmation the K4 filter + budget truncation never drop
  a relevant node.

### Streamed authoring (2026-06-22)

> Close-out of `streamed-authoring`. The persona-authoring draft now **streams**
> over SSE, so the persona visibly forms within ~1–2s of submitting
> (time-to-first-token) instead of behind a 30–60s blank spinner — the
> authoring-side counterpart to v1's async-create fix. **Transport + UX only:**
> the produced draft is contract-identical to the blocking path (the validated
> `AuthoringDraft`), and schema-validation + the retry safety net are unchanged.
> No new dependency, no migration, no contract change.

#### Added
- **SSE-streamed authoring** — `POST /v1/personas/author` and `/author/refine`
  now return `text/event-stream`: `chunk` deltas as the model generates, a
  visible `retry` event when the validation-repair re-stream fires, then the
  validated `AuthoringDraft` as the terminal `draft` event + a `done` sentinel.
  The web author wizard consumes the stream via the shared `consumeSSE` helper
  and paints the draft forming live (raw-text preview) before resolving to the
  editable review form. The streamed terminal draft is byte-equivalent to the
  blocking path; the parse → validate → retry-once safety net is shared between
  both paths so the model-agnosticism contract cannot drift.
- **Cancel-safe authoring** — the wizard wires an `AbortController` aborted on
  unmount; navigating away cancels the upstream request. A cancelled or failed
  stream produces no terminal draft — so no credit is deducted — and a stream
  that drops before the draft surfaces a retry (no silent partial draft).

#### Changed
- **Authoring credit deducts after the terminal draft, not up front** — the flat
  authoring credit is deducted only once the validated draft is produced (after a
  clean stream), mirroring chat's deduct-after-completion. The top-of-route
  pre-flight 402 + rate-limit + the refinement-round backstop still run *before*
  streaming begins. A validation-exhausted draft (best-effort YAML + errors) is a
  delivered draft and still charges, unchanged.
- **OpenAPI / web client** — `/author` + `/author/refine` are now SSE-primary;
  the `AuthoringDraft` type is preserved in the generated client and consumed as
  the terminal SSE payload. Graceful degrade reads that terminal payload — there
  is no separate REST fallback.

### Voice — STT cost gating (2026-06-22)

> Stop billing Deepgram for the entire call. The agent runner streamed **every**
> inbound mic frame to Deepgram, ungated, for the whole call — including the time
> the persona is speaking and every idle pause (often half+ of a listen-heavy
> call). V8 gates the billed stream on conversational state so Deepgram bills
> ≈ the user's turn, not the call duration, with no transcription regression.
> Backend-internal (`persona-voice`); no web/api/runtime/core change; **zero new
> dependency, zero migration, zero new env var.**

#### Added
- **Split-tee cost gate.** `V1STTStreamSeamAdapter.push_audio` now splits its tee through an optional `StreamGate`: the Silero VAD **always** receives every frame (barge-in onset is local + free and must never be starved), while the billed Deepgram leg is fed only when the gate is open. An absent gate is permanently open — pre-V8 behaviour, so every existing call site is unchanged.
- **Idle-gate (shipped).** `IdleAwareGate` streams the billed leg **only during the user's turn** (`USER_SPEAKING` / `PROCESSING`); closed during persona-speaking + listening idle + preparing. ~85 % streamed-seconds reduction on a listen-heavy profile. (The simpler `PersonaSpeakingGate` — close only while the persona speaks, ~79 % — is retained as the validated building block.)
- **Ring-buffer-on-reopen.** A shared pre-roll ring (`reopen_preroll_ms`, 300 ms in the runner) buffers audio while the gate is closed and flushes the capped tail on every closed→open transition — so the barge-in opening (the ~250 ms confirm window) and the post-idle first word reach Deepgram intact. Fixes the only fidelity regression the gate would otherwise introduce.
- **Cost instrument + re-base.** `V1STTStreamSeamAdapter.streamed_seconds` counts the billed audio; `VoiceLog.stt_streamed_seconds` (additive, nullable) carries it; `compute_stt_total_cents(streamed_seconds, cents_per_minute)` re-bases `stt_total_cents` off streamed audio rather than wall-clock duration.
- **Empirical A/B harness + committed live gate.** `persona_voice.stt.cost_harness` (deterministic Axis-1 cost model + gate-faithful validation); a committed `@external` Deepgram replay (`tests/external/test_v8_cost_gating_live.py`) over rendered fixtures (`tests/fixtures/v8_corpus/`) asserting first-word-preserved + WER ≤ ungated + 2.0 pp at the reopen/resume points.

#### Fixed
- **Barge-in while the persona is speaking** (real-voice operator-pass finding). The V2 TTS-mute-window suppressed Silero's onset for the whole persona-speaking window, so a *real* barge-in never armed (persona couldn't be interrupted — a pre-existing V4 limit) and — once V8 gated the billed stream during persona speech — the user's interrupting words were withheld from Deepgram until the persona finished ("thank you" → "q"). The mute-window is now **opt-in, default off** (`PERSONA_STT_SILERO_ECHO_MUTE_WHILE_SPEAKING`): the onset reaches the orchestrator, its unchanged confirm-window decides the interrupt, the gate reopens, and the 300 ms ring delivers the opening — one root fix restoring both the persona-stop and the transcription. Echo is handled by browser/transport AEC; re-enable the mute only on a no-AEC deployment. (Regression test added; the prior synthetic harness missed this by manually reopening the gate.)

#### Notes
- The within-user-turn onset gate (and a `Finalize`-based variant) were **measured sub-threshold** (≈ 6 % marginal vs a 15 % bar) and risk WER on the user's own speech, so they are **declined** as a documented seam, not built.
- The STT stream closes promptly on every true call-end (hang-up / switch / reload-teardown) — pinned by an end-to-end teardown regression test (no lingering billed stream).

### Web v1 redesign — global notification + consent systems (2026-06-21)

> Close-out of the web v1 production redesign. The screen/shell restyle landed
> incrementally; this entry records the two final app-wide systems that complete
> it — every user-facing message now flows through one notification façade, and
> every confirmation through one consent dialog. **Zero native browser dialogs
> remain.** No new dependency.

#### Added
- **Global notifications (`useNotify` / `NotificationProvider`)** — a single façade over the existing toast layer that *also* feeds a persistent **bell center**: a client-side feed (capped at 30, `localStorage`, no backend) shown in a base-ui popover from the sidebar header (desktop) + mobile header. Levels are `success` / `error` / `info` / `warning`; error + success persist to the bell by default, transient info/warning don't (override per call). Wired to real events — chat document attach/error and delete/duplicate successes across conversations, personas, and artifacts.
- **Global consent (`useConfirm` / `ConfirmProvider`)** — an async `confirm()` that resolves off one token-styled base-ui dialog (destructive "danger" tone for delete flows), replacing every native `window.confirm()`.

#### Changed
- **All 6 native `confirm()` calls replaced** (conversation / persona / artifact delete + persona duplicate ×2) — a repo grep of `packages/web` now shows zero `alert(` / `confirm(` / `window.confirm(`.
- **Chat notifications unified** — the composer/chat surface no longer calls the toast layer directly; consequential events persist in the bell, transient validation toasts without it.
- **Fully internationalised** — new `confirm` + `notifications` next-intl namespaces; every call site passes localised copy.

### Persistent voice experience (2026-06-22)

#### Added

- **Persistent voice experience** (`persistent-voice-experience`, web) — a voice
  call now behaves like a real call. The call is hoisted into an app-level session
  mounted once in the shell, above the App Router, so it survives in-app
  navigation (the `Room` + audio sinks + mic live in the layout provider, never a
  route). A draggable, collapsible **mini call-bar** controls the call from
  anywhere; **active-call indicators** mark the on-call persona on its card and
  chat header with one-tap return; exactly **one call at a time** with an
  **end-and-switch** confirm (serialized teardown — never two rooms);
  **resume-after-reload** offers (a prompt, never a silent auto-dial; a fresh call
  on the same conversation, bounded by a freshness window); input controls add
  **push-to-talk** for noisy environments (persisted preference); and a finished
  call leaves a **web-derived recap** ("call ended · N min · view transcript") in
  the chat thread. Pure `packages/web` — consumes the V1–V6 voice stack unchanged.
  Deferred to documented forward seams: warm reconnect to the same room, the
  durable `origin=call` marker (→ call-history spec), the TTS-unavailable wire
  signal, and in-app input-device selection.

### Added — Shared knowledge-graph store (`persona.graph`, direction-3 foundation)

The user-scoped "bigger brain" all of a user's personas read from and write to —
the trunk of the K-track (K1 hybrid retrieval, K2 write paths, K3 graph-aware
prompts, K4 wellbeing, K5 graph UI build on it).

- **Concept-node + typed-link model** (`persona.graph.models`): frozen-Pydantic
  `ConceptNode` (a sibling of `PersonaChunk`, not a subclass), `TypedLink` over four
  relationships (semantic / entity / temporal / causal), `CanonicalEntity` + alias
  set, `NodeProvenance` (accumulation trail with `superseded_content` for
  contradiction history). Nodes **accumulate via merge**, not duplicate.
- **Two-layer storage**: Postgres is the source of truth (`graph_nodes` /
  `graph_edges` / `graph_entities` / `graph_node_entities`, float32 `vector(384)` +
  generated FTS, **direct-`owner_id` RLS**, Alembic migration extending Spec 07); an
  optional **turbovec** quantized in-RAM dense index (`persona-core[turbovec]`,
  lazy-imported, never a hard dep). **pgvector is the default + only-wired prod path.**
- **Dense retrieval**: a `GraphIndex` protocol with both adapters; on the turbovec
  path the exact-rerank is structurally mandatory (ANN→float32-rerank→top-k) so
  **4-bit recall@10 ≥ 0.95** holds (validated; CI-gated). Identical allowlist
  semantics across backends.
- **Merge engine** (the coherence heart): canonicalise → extend-vs-create (tuned,
  config-driven threshold) → accumulate-with-provenance (no silent overwrite) →
  auto semantic links (capped, re-evaluated on extend) → typed-link attachment.
  Idempotent re-merge.
- **Canonical entity resolution**: deterministic three-way verdict
  (`MERGE`/`SEPARATE`/`AMBIGUOUS`) — Fellegi-Sunter zones over embedding + lexical
  (hand-rolled Jaro-Winkler), **LLM-free** (K2 owns the judge on the ambiguous band);
  a config-driven sweep harness for re-tuning.
- **`GraphStore`** assembly with same-path index sync (Postgres-authoritative;
  index drift surfaced, recoverable via `rebuild_index`), one `AuditEvent` per
  mutation (`store="knowledge_graph"`), and the K1 read legs (dense / FTS / typed
  neighbours).
- Config via `PERSONA_GRAPH_*` (thresholds, index backend, bit-width, rerank-N).

### Persona-initiated messages — the origination primitive (2026-06-22)

> The system-wide primitive that lets a persona **originate** a message — one it
> produces with no preceding user turn ("I've finished the task you asked for") —
> as a first-class conversation + memory citizen, delivered through a
> one-boundary-many-deliverers seam. The connectors track (Telegram/Discord/…) and
> direction-4 autonomy are the *consumers* of this primitive; they drive one pipe,
> not N channel-specific paths. **Zero new dependency. One additive column, no new
> table. No regression to the request/response path.**

#### Added
- **The originated-message model (`persona-core`)** — a frozen `OriginatedMessage`
  outbound type + `PersonaIdentityTag` (the name/visual tag that survives delivery
  so the user sees *which* persona is speaking). An originated message persists as
  a first-class `assistant` message marked by a `metadata["originated"]` marker
  (in-core) ↔ a real `messages.originated` BOOLEAN column (DB), and is written to
  episodic memory the same as a reply — the persona remembers reaching out.
- **The trigger-agnostic origination capability (`persona.originator.Originator`)**
  — the single callable the runtime invokes (build → record → deliver → report).
  It knows nothing about *why* it was called, so the within-runtime conclusion and
  (later) direction-4's autonomous trigger drive the *same* interface — the
  direction-4 seam, proven by construction.
- **The delivery boundary (`persona.delivery.MessageDeliverer`)** — a minimal
  `@runtime_checkable` port + `DeliveryOutcome` / `DeliveryResult`. One boundary,
  many deliverers: the web app implements it now; connectors will next.
- **The web-app deliverer + delivery routing (`persona-api`)** — deliver inline on
  a live run's open stream, else present-on-next-open (persisted; never dropped).
  The router picks exactly one channel (web home) — no double-delivery, no silent
  drop, no platform branching.
- **RLS-scoped persistence with airtight ownership** — a persona originates ONLY to
  the user who owns it; a cross-tenant attempt raises `OriginationForbiddenError`
  before any write (fail-loud, no half-write), with RLS as the production backstop.
- **Within-runtime origination (`PERSONA_API_WITHIN_RUNTIME_ORIGINATION`, default
  OFF)** — when enabled, a completed agentic run originates its conclusion as a
  delivered, persisted message, pushed inline on the run's own open stream. Default
  OFF: the primitive is shipped + proven; *when* a persona originates is a
  downstream/direction-4 decision.

#### Migration
- `013_add_message_originated` — idempotent `ADD COLUMN IF NOT EXISTS
  messages.originated BOOLEAN NOT NULL DEFAULT false` (the `role` CHECK is
  untouched — `role` = who speaks, `originated` = self-initiated vs solicited).

---

## [1.0.0] - 2026-06-20

> **Open Persona v1.0 — first stable release.** The complete four-layer platform: a
> source-available core (`persona-core`, MIT) with four typed memory stores
> (identity / self_facts / worldview / episodic), versioned append-only history, and a
> CLI; the runtime (`persona-runtime`, MIT) conversation loop, rule-based tier router,
> and agentic engine; the hosted FastAPI service (`persona-api`, PolyForm-NC) with
> Postgres + pgvector, per-tenant RLS, SSE streaming, document & image ingestion, a
> sandboxed code-execution tool, and credit metering; the real-time voice service
> (`persona-voice`, MIT, WebRTC via LiveKit); and the Next.js web app (`persona-web`,
> PolyForm-NC). Open-core editions ship via `PERSONA_EDITION=community|cloud`. All five
> packages are versioned at 1.0.0; `persona-core` / `persona-runtime` / `persona-voice`
> are published to PyPI. The entries below are the cumulative feature history rolled
> into this release.

### Prebuilt Personas — editable starters, no authoring required (code-complete 2026-06-18)

> The new-persona screen now leads with a curated row of **flagship, fully-structured
> starter personas**. Pick one, edit every field in place, and create it **directly** —
> the edited structure posts straight to `POST /v1/personas` with **no LLM authoring
> call and no minutes-long wait** (~1–3s). Avatar + voice are the only generated
> assets, produced on-create by the existing async enrichment so they **follow your
> edits**. The "describe your own" drafter, "start from scratch", and "Edit YAML" paths
> all remain. **No new create endpoint; no API-contract change.**

#### Added
- **24 flagship structured starters** (`persona-examples.ts`) across six categories — each a complete v1.0 persona (identity / self_facts / worldview / constraints + real `tools`/`skills`/MCP wiring). Backgrounds are capability-forward and reference roadmap ambitions (autonomy, proactive messaging, the knowledge graph) **as prose only** — never as functional wiring. A dataset-integrity test imports the live capability palettes so a faked capability fails CI.
- **Quick-edit preview + direct create** — picking a starter (or "start from scratch") reveals an inline quick-edit card (the design's "Choose & edit" draft): edit name / role / background / self_facts / worldview lines / constraints (safety pinned), then **Create directly** (no `/author` call), or **Open full editor** for tools / skills / MCP / voice / routing. The quick edits carry over into the full editor (shared `doc`); the drafter "describe your own" path is preserved.
- **Design-matched starter cards** — compact per-persona identity-coloured avatar + name + role (via `PersonaAvatar`), replacing the earlier editorial card.
- **Client-side schema validation** (`personaDocSchema`, zod) — the edited structure is validated against the v1 schema before submit, surfacing field-scoped errors; the server 422 remains the final authority.
- **Mandatory safety constraint, enforced everywhere** — a single shared `SAFETY_CONSTRAINT` (Python source of truth in `persona-core`, mirrored once in web with a byte-match drift-guard test). It is pinned non-removable in the editor, re-asserted client-side at assembly, and — the floor — re-asserted **server-side at the create service boundary** (`ensure_safety_constraint`) on every create/update path, including the stored YAML the runtime reloads from. A persona can no longer be created or updated without it.

#### Changed
- **The new-persona screen leads with "describe your own" + "start from scratch"** on top, with the starter suggestions below; picking a starter reveals the quick-edit card inline (rather than seeding the drafter textarea or jumping straight to the full editor).
- **`persona-examples.ts` is now the single canonical roster** — the starter `seed` (drafter input) is derived from and coherence-tested against each starter's structured identity; no divergent second example set.

### Local dev DB safety — integration-test guard + self-healing bootstrap (2026-06-19)

> Fixes the recurring "local Postgres suddenly empty / missing tables" failures. Dev-only; no product change.

#### Added
- **Integration-test safety gate** (`packages/api/tests/conftest.py`) — the destructive Postgres fixtures (`DROP SCHEMA public CASCADE`) now refuse to run unless the target DB name ends in `_test` or `PERSONA_TEST_DB=1` is set, so a stray `pytest -m integration` in a dev shell can no longer wipe the dev schema. CI opts in via `PERSONA_TEST_DB=1`.
- **Self-healing local bootstrap** (`packages/api/run-local.sh`) — on launch, probe Postgres then idempotently `alembic upgrade head` + grant the `persona_app` RLS role, so a fresh/wiped `pgdata` volume comes up fully working with no manual steps.

#### Changed
- **Pinned Compose project name** (`name: open-persona`) so running from any git worktree shares one `pgdata` volume instead of spawning an empty per-directory one; made the host port configurable via `${POSTGRES_HOST_PORT:-5432}`.

### Open-Core Editions — community / cloud + per-package relicense (code-complete 2026-06-18)

> The monorepo becomes a clean open-core project: an **MIT engine**
> (`persona-core` / `persona-runtime` / `persona-voice`) + a **source-available
> app** (`persona-api` / `persona-web`, PolyForm Noncommercial 1.0.0), where the
> commercial layer (auth, credits, multi-tenant RLS) is **edition-gated off by
> default**. A single `PERSONA_EDITION` switch selects the runtime layer.
> **community** (the default) is a clone-and-run local self-host — no auth, no
> credits, no Postgres/Docker (SQLite + Chroma). **cloud** reproduces today's
> hosted behavior exactly (Clerk auth, Postgres RLS, metered credits). **No
> product feature change; no DB migration; no cloud regression.**

#### Added
- **`PERSONA_EDITION=community|cloud`** (default `community`) — one switch read by api, web, and voice, driving every commercial seam.
- **`OwnerResolver` seam (api)** — `CommunityOwnerResolver` (a fixed local owner, no JWT) / `CloudOwnerResolver` (the existing Clerk-JWT path). Downstream RLS scoping + the persona-ownership pre-flight consume `owner_id` unchanged.
- **`CreditsPolicy` seam (api)** — `UnlimitedCreditsPolicy` (community no-op) / `MeteredCreditsPolicy` (the existing ledger). Injected via `app.state`; every metered call site consumes the interface.
- **Community persistence** — a SQLite relational store (no RLS — single owner) built from a dialect-aware variant of the canonical schema, with `metadata.create_all` schema-create (no Alembic), `PRAGMA foreign_keys=ON`, and an idempotent fixed-owner seed; typed-memory vectors go to Chroma (the `memory_chunks` pgvector table is cloud-only).
- **Safety guard** — a community/no-auth process refuses to start on a non-loopback bind unless `PERSONA_ALLOW_PUBLIC_NOAUTH=1` (fail-safe against an accidentally-exposed open instance).
- **Web `@/auth` seam** — all Clerk usage isolated behind `@/auth` + `/server` + `/provider` + `/middleware`, selected at build by `turbopack.resolveAlias`; a community build is provably Clerk-free (a module-graph gate + a build-artifact grep + scoped import isolation; `pnpm check:clerk-free`). Sign-in/up isolated as thin cloud components.
- **Voice edition stance** — the voice token endpoint is no-auth/no-credits in community (fixed local owner), Clerk-verified in cloud.
- **License boundary CI gate** — an `import-linter` contract proving the MIT engine never imports the PolyForm-NC app (`uv run lint-imports`).

#### Changed
- **Relicensed per package:** `persona-core` / `persona-runtime` / `persona-voice` → **MIT**; `persona-api` / `persona-web` → **PolyForm Noncommercial 1.0.0** (added the previously-missing `LICENSE` files + license metadata). Root README carries the honest per-package licensing table + open-core framing.
- **`models.py` JSONB columns** now use `JSON().with_variant(JSONB, "postgresql")` — byte-identical JSONB DDL on Postgres (a test asserts the empty cloud-DDL diff), generic JSON on SQLite.

### Voice Experience Enhancements — Persona-Initiated Greeting · Per-Persona Language Routing (Spec 32; code-complete 2026-06-16, operator pass runs jointly with V6's deferred live pass)

> A V6 fast-follow built on the V6 branch: two findings from V6's live bring-up that the frontend alone can't fix. **(A) Ring-until-greeting** — the persona *answers the phone*: the call rings while it generates turn 0 (its opening line) with the cold path warmed off-loop, then it speaks first; the mic stays gated until the greeting finishes. **(B) Per-persona declared-language routing** — each call runs in the persona's `identity.language_default`: STT pinned to the right Deepgram model+code, TTS spoken with the right Cartesia language, and the LLM instructed to reply in that language; English is the fail-soft default. **No schema change, no migration**; additive throughout.

#### Added (persona-core)
- **Voice-language capability registry** (`language_capability.py`): the centralized spine — a canonical `Language` tag (a mirrored subset of Pipecat's enum, BSD-2 / Daily) + `normalize` (collapsing `nb`/`nb-NO`/`nn`/`nn-NO` → the served `no`, with BCP-47 base-code fallback), per-provider STT/TTS resolution, `is_serviceable` for author-time validation, and a typed `LanguageFallbackEvent`. Unsupported `(language, provider)` resolves to English — never a crash, never a silent wrong-language call.

#### Added (persona-voice)
- **Greet-first opening** (`turn_taking/states.py`, `orchestrator.py`, `agent/runner.py`): a new `PREPARING` conversational state + the legal turn-0 entry (`PREPARING --model_first_audio--> PERSONA_SPEAKING`) and degrade (`PREPARING --reset--> LISTENING`); `begin_greeting` generates turn 0 from the persona's identity with no user input, gating on the embedder warm-up (bounded by the ring) and degrading to the user's floor if it stalls (never rings forever).
- **Embedder warm-up off the loop** (`agent/warmup.py`): a one-shot threaded `encode()` at session build — the *root* fix for the first-turn truncation (the cold `bge` load no longer blocks the agent loop).
- **Per-call language plan** (`agent/language.py`): resolves `language_default` once into the STT route (nova-3 + `no` for Norwegian), the TTS route, and the reply language (keyed on what TTS will actually speak, so a TTS fall-back also steers the reply text); pinned into the Deepgram + Cartesia configs before the sockets open.
- **Data-channel `preparing` frame + graceful onset handling**: the greet-first ring signal the client binds to; a stray user onset during `PREPARING` is logged and dropped (the mic-gate hand-off is safe-by-construction at the FSM + recover-don't-crash at the orchestrator).
- **Env-tunable greet bounds** (`PERSONA_VOICE_GREET_WARMUP_TIMEOUT_S`, `PERSONA_VOICE_GREET_TIMEOUT_S`).

#### Added (persona-runtime)
- **Reply-language injection** (`prompt.py`): a "respond in {language}" directive injected into the system prompt when the resolved language isn't English (mandatory for turn 0, which has no user input to mirror); English personas are unchanged.

#### Added (persona-web)
- **`ringing` call phase + greet-first mic-gate** (`lib/voice/call-state.ts`, `voice-events.ts`, `use-voice-call.ts`): the ring lifecycle (preparing → greeting → un-gate) as a pure reducer; the mic is held gated until the greeting finishes (un-gate at completion), with a client-side ring backstop.
- **Ringing surface** (`voice-call-surface.tsx`): a "Calling {persona}…" state distinct from `connecting`.
- **Author-time language hint** (`lib/voice/language-support.ts`, `persona-form.tsx`): an inline warning when a declared language the providers can't serve is entered (client mirror of the registry), complementing the API-side warning at persona create/update.

#### Fixed
- **Norwegian voice calls** — STT was force-decoding Norwegian as English (global `language_hint`) and Cartesia spoke Norwegian text with English phonetics (no `language` param). Both now route per the persona's declared language; the Deepgram websocket no longer 400s on `nb` (normalized to `no`).

### Persona Decision Controls & Transparency — Routing + Autonomy (Spec 31; close-out 2026-06-16, pending sign-off)

> **The web counterpart to intelligent routing (Spec 23) + proactive autonomy (Spec 21):** makes *how a persona decides* controllable and transparent. Three surfaces — routing controls + routing transparency (the net-new core) and the wiring of the already-built autonomy controls — plus one additive, migration-free backend touch surfacing the routing decision on the chat `done` event. **Autonomy-prompts-in-chat consumes Spec 30's merged chat-proactive rail (no fork — the `proposal`-absent clarification path).** No new dependencies; no migration; backward-compatible (personas without `routing.intelligent` are byte-identical).

#### Added (persona-web)
- **Routing controls** (`components/personas/routing-section.tsx`, composed into `persona-form.tsx`): enable intelligent routing per persona + an **intent preset** (cost / balanced / quality / speed) that maps to the cost/quality/latency weights, with raw weights behind an Advanced disclosure (auto-opens on a "Custom" vector); budget-cap inputs (per-turn/session/day) where a blank input is *unset* (never 0) and a per-day cap carries the Spec-23 fail-loud warning. Binds `routing.intelligent`/`routing.budget` via the existing persona YAML PATCH (`persona-draft.ts` `readRouting`/`writeRouting` + the locked preset table).
- **Routing transparency**: the tier badge (`tier-badge.tsx`) expands into a progressive-disclosure chip — *chose `<model>` — <reason>* (templated client-side from the structured decision; raw score vector never on the wire), with an honest "tier default — live model data unavailable" fallback; a new **budget indicator** (`budget-indicator.tsx`) shows session-spend-vs-cap with an "approaching" note at the real 0.8 soft-ramp knee and an honest per-day fail-loud note.
- **Autonomy controls wiring**: the previously-unwired `autonomy-consent-section.tsx` is surfaced inside `PersonaEditor`, gated on `personaId` (edit context only); the consent tri-state (grant/decline/revoke) round-trips via a new typed `setConsent` server action over `PATCH /{id}/consent`. Autonomy clarifications now appear in chat via Spec 30's rail (the `proposal`-absent `asking_user` path), answered inline.

#### Added (persona-api)
- **Additive, SEPARATE `routing` (D-31-1) + `budget` (D-31-2) fields on the chat `done` event** (`DoneEvent` + `RoutingSummary` + `BudgetSnapshot` in `schemas/responses.py`): a concise `{chosen_model, dominant_factor, model_fallback_engaged, model_fallback_reason}` summary (captured from the tier event) + a per-session budget snapshot (read post-turn so it includes the current turn). Both omitted on rule-based turns (back-compat). No migration; SSE shapes hand-mirrored in `sse-types.ts`.

#### Added (persona-runtime)
- **`RunEvent.tier(tier, routing=…)`** carries an optional concise model-decision summary when intelligent model-within-tier selection ran (absent ⇒ bare-tier payload). New read-only **`ConversationLoop.session_spent_cents`** property + **`budget_snapshot()`** (per-session spend + configured caps; `None` when routing off / no cap). All additive; the raw score vector stays on the JSONL TurnLog.

### Frontend Capabilities — Tools · Skills · MCP + Bring-Your-Own MCP (Spec 30; close-out 2026-06-16, pending sign-off)

> The web counterpart to the merged 26/27/28 backend: the unified tool + skill + MCP capability model is now reflected and controllable in the frontend, plus a net-new **bring-your-own MCP** slice with a security-load-bearing SSRF guard + credential encryption. Additive across all layers — existing personas, the tools/skills selection, and chat rendering are unaffected. One migration (`009`); one new direct API dep (`cryptography`, already locked).

#### Added (persona-core)
- **Capability-kind resolver** (`persona/tools/kind.py`, `Toolbox.kind_for`) — maps a dispatched tool name to its source (`builtin` / `skill` / `mcp:builtin` / `mcp:optional`); unknown → `builtin` (total, never raises). One authoritative home for the taxonomy.
- **SSRF guard for bring-your-own MCP** (`persona/tools/mcp/ssrf.py`) — `assert_url_allowed` (eager) + a **resolve-then-pin** httpx transport (`pinned_httpx_client_factory`) that re-resolves + re-validates on **every request** (defeats DNS rebinding *and* redirect-to-internal), connecting to the validated IP while preserving Host + TLS SNI. https-only; blocks loopback / RFC1918 / link-local (incl. `169.254.169.254`) / ULA / CGNAT / reserved / multicast, with IPv4-mapped + NAT64 unwrapping. Stdlib only (no SSRF dependency). New domain exception `MCPUrlNotAllowedError`.
- **`MCPClient`** gains `enforce_ssrf` (opt-in; off for trusted loopback built-ins) + `headers` (bearer auth for BYO servers). `build_default_toolbox` gains `extra_mcp_clients` — pre-built BYO clients whose tools are auto-allowed (the persona↔server assignment is the authorization).

#### Added (persona-api)
- **Bring-your-own MCP**: `user_mcp_servers` + `persona_mcp_assignments` tables (migration `009`, RLS-forced + policied), `persona_api/mcp/crypto.py` (Fernet/MultiFernet credential encryption at rest — `MCP_CREDENTIAL_KEY`), `persona_api/mcp/store.py` (CRUD + test-connection/discovery + assignment, SSRF-validated, credentials never returned/logged), and the `/v1/mcp-servers` + `/v1/personas/{id}/mcp-servers/{server_id}` routes. `RuntimeFactory` resolves a persona's assigned BYO servers and connects them SSRF-pinned on the live runtime path.
- **`kind` on tool events** — `RunEvent.tool_calling`/`tool_result` (and `responses.py` `ToolCallEvent`/`ToolResultEvent`) carry an additive `kind` (one change badges both the chat and run SSE streams).
- **General chat-proactive-question rail** — `ProactiveQuestion` gains a source-agnostic `proposal {kind, name, provider?, action}`; the chat SSE carries `asking_user` (tool-gap / MCP-gap consent offers) so the web can wire accept → grant/assign → retry. Spec 31 consumes the rail. The tool-consent path now admits catalog-valid `mcp:<server>` grants.
- **`GET /v1/mcp-catalog`** — the built-in MCP servers for the capability-management UI. New direct dependency `cryptography>=43,<49` (already in `uv.lock` via `python-jose`).

#### Added (persona-web)
- **Unified capability management** in the persona editor — built-in tools + skills + **MCP servers** selectable as one set, the combined-capability count (~10 soft cap) communicated, and the recommender's provider-tagged picks surfaced as suggested-and-explained (user-triggered, cost-aware).
- **Badged in-chat rendering** — `tool-call-card` badges each call by source and names the MCP server.
- **In-chat consent rail** — the runtime gap prompt renders inline (reusing `ask-user-prompt`), accept grants the capability and re-sends the message (surface-and-retry).
- **Bring-your-own MCP manager** — add (URL + optional bearer token) / test-connection / assign-to-persona / delete; credentials entered but never displayed back.

#### Security
- BYO-MCP credentials are encrypted at rest (Fernet/MultiFernet), never returned over the API (only `has_credential`), decrypted transiently for the connect only, and never logged (asserted). The SSRF guard rides the **live** runtime connect path (per-request resolve-then-pin), not just test-connection — closing the validate-at-test / rebind-at-use TOCTOU and redirect-based bypasses. RLS isolates BYO servers per owner (verified through the non-superuser `persona_app` role).
### Persona Avatar Auto-Generation (close-out pending; operator pass pending sign-off)

> When a persona is created from the builder's details and no avatar is supplied, the system **auto-generates a role-appropriate, demographic-safe avatar** through the existing image-generation pipeline, persists it, and sets `avatar_url`. The user can still replace it by upload (existing path — a user-supplied avatar always wins). Generation is **fail-soft**: if image generation is unavailable, content-rejected, errors, or times out, the persona is still created with `avatar_url=null` and the build succeeds (the initials/identicon default renders). Purely additive — no schema field, no migration, existing create/PATCH/upload behavior unchanged. **Zero new dependencies.**

#### Added (persona-core)
- **`craft_avatar_prompt`** (`persona/imagegen/avatar_prompt.py`): a deterministic, demographic-safe avatar-prompt crafter. Builds a role-anchored professional portrait from the persona's declared identity. Demographic handling is **declared-first**: `role` is the professional anchor, `visual_style` is the only channel through which apparent gender/age/appearance enters the prompt, `name` is omitted (no name-based stereotyping, no PII), and `background` prose is never parsed (the demographic-leakage vector). Pure function — the same identity yields a byte-identical prompt; it emits only professional-portrait vocabulary, so it passes the hard-line categorical filter clean by construction. Exported from `persona.imagegen`.

#### Added (persona-api)
- **Build-time avatar generation hook** in `POST /v1/personas`: after the persona row is committed and only when no `avatar_url` was supplied, crafts the prompt → generates → sets `avatar_url` to the served uploads path. Bounded by a wall-clock timeout (`PERSONA_API_AVATAR_GEN_TIMEOUT_S`, default 25s) and fail-soft across the full failure surface (backend-absent, content-rejection, provider error, timeout, unexpected) → `avatar_url=null` + a zero-cost system audit event; never raises into create.
- **`imagegen.service.generate_avatar`**: a free build-time generation entry — no credit deduct, no per-user concurrency lock (D-29-2). Runs the hard-line categorical filter explicitly (the service path otherwise does not) as the demographic-safety backstop for a verbatim declared `visual_style`; emits a JSONL audit event per outcome (no migration).
- **`persona_service.set_avatar_url`**: a narrow RLS-scoped presentation-field write (no YAML re-validate / memory re-index).
- **`PERSONA_API_AVATAR_GEN_TIMEOUT_S`** config (`APIConfig`, default 25.0s).

### MCP v1 — Built-in MCP Servers + Curated Catalog + Authoring Integration (Spec 27; close-out 2026-06-15, pending sign-off)

> **Three coupled deliverables:** (1) the Spec-04 MCP infrastructure **verified end-to-end** against a real Streamable-HTTP server (no wiring gap — Spec 15 §2.9 pattern checked); (2) **4 built-in MCP servers** (zero → four) shipped as thin FastMCP Streamable-HTTP subprocesses, **lazily spawned** and loopback-only; and (3) **persona-driven MCP selection** — the Spec-26 recommender generalised to rank built-in tools, skills, and MCP servers together, plus a runtime MCP-gap proactive-consent prompt. Purely additive to Spec 04 + Spec 26; existing personas are unaffected. **Zero new dependencies** (`mcp`/`tzdata`/`httpx` already present).

#### Added (persona-core)
- **Built-in MCP servers** (`persona/tools/mcp/builtin/`): `time` (delegates to the in-tree `datetime` tool), `calculator` (wraps the Spec-26 hardened AST evaluator), `filesystem` (sandboxed — delegates to `file_read`/`file_write` + their `resolve_sandbox_path` guard), and `weather` (open-meteo, no API key; opt-in). Each is a thin `FastMCP(transport="streamable-http")` app reusing already-tested logic. Launched via `python -m persona.tools.mcp.builtin <name>`.
- **Declarative MCP catalog** (`persona/tools/mcp/catalog.toml` + `catalog.py`) — per-server metadata (kind/risk/default-enabled/required-env/keywords); the precursor to the deferred federated registry (100% local, zero-network, mirrors the Spec-24 skills catalog). `fetch`/`github` are catalogued as bring-your-own external servers (Persona ships no code for them).
- **`PERSONA_MCP_BUILTIN_ENABLED`** + **`PERSONA_MCP_BUILTIN_UID`** config (`persona/config.py`). New domain exception `MCPBuiltinServerError`.

#### Added (persona-runtime)
- **`proactive_mcp_gap.py`** — `detect_mcp_gap` (post-generation; a capability-gap phrase + a catalog MCP-server keyword for a server the persona lacks) + `build_mcp_gap_question` (Spec-21 3+1 consent offer). Wired into `ConversationLoop.turn` as a post-generation hook, **mutually exclusive** with the Spec-26 tool-gap hook (one offer per turn).
- **TurnLog MCP telemetry** — `mcp_invocations` + `mcp_unavailable_requested` (runtime-only JSONL; no migration, same discipline as the Spec-26 tool-gap fields).

#### Added (persona-api)
- **Lazy per-server MCP supervisor** (`persona_api/mcp/builtin_launcher.py`) — registers enabled built-ins at startup but spawns **nothing** until a persona resolves an `mcp:<server>:` tool; one-time process-wide cold spawn, re-spawn-on-resolution restart, loopback-only bind, optional privilege-drop, shutdown reaping (mirrors the Spec-12 sandbox subprocess lifecycle). Wired into `RuntimeFactory` (`build_default_toolbox` gains an additive `extra_mcp_servers` kwarg).
- **Unified capability recommender** — `recommend_capabilities_for_persona` ranks built-in tools ∪ skills ∪ MCP servers in one mid-tier call, provider-tagged, capped at the **combined** ≤10 (D-27-13). New `POST /v1/personas/recommend-capabilities` route. `ToolRecommendation` gains a defaulted `provider` field (the D-26-10 unification; the Spec-26 shape stays a forward-compatible subset).
### Rich Tool Output Delivery — Backend Persister + Inline File Cards + Right-Panel Renderer (Spec 28; close-out 2026-06-15, pending sign-off)

> **Three coupled deliverables:** (1) a hexagonal **`WorkspacePersister`** giving every byte-producing tool (`generate_image`, `file_write`, `code_execution` outputs, new `render_diagram`) a persisted `workspace_path` + `mime_type` + downloadable ref; (2) an inline **`FileCard`** (Anthropic-style) in chat; (3) a sliding **right-panel renderer** for 10 formats with a rendered↔raw toggle. Closes the Spec 25 §2.9 byte→UI delivery gap. **Additive only** — `persister=None` reproduces today's exact `ToolResult` (criterion #9). **Zero DB migrations** (telemetry → F5 sidecars), **zero new core/api Python deps** (diagrams render client-side). Operator pass **9/9 live, 0 FAIL** (backend pre-drive 4/4 + Playwright UI 5/5; [`operator_pass_2026_06_15.log`](docs/specs/phase2/spec_28/evidence/)). `mypy --strict` core + `mypy` api + `ruff` clean; web `tsc` + `biome` + `no-literals` + `vitest` clean.

#### Added (persona-core)
- **`WorkspacePersister` Protocol** (`persona/tools/workspace_persister.py`) + frozen **`PersistedArtifact`** (`persona/schema/tools.py`): `workspace_path` / `mime_type` / `size_bytes` / `rendered_inline`. Storage-agnostic port (S3 adapter is a v0.3 drop-in).
- **`ToolResult.artifacts: tuple[PersistedArtifact, ...] = ()`** — one typed field; default-empty = wire-compatible with the pre-Spec-28 shape.
- **`render_diagram` built-in tool** (`persona/tools/builtin/render_diagram.py`) — persists Mermaid / Graphviz DOT **source** (MIME `text/vnd.mermaid` / `text/vnd.graphviz`); lenient (no server-side parser); rendered client-side. Catalog entry added.
- `generate_image` + `file_write` gain an optional `persister`; `code_execution` surfaces its remote produced-files into the same `artifacts` tuple (keeps the D-17-X file-copy callback).

#### Added (persona-runtime)
- `RunEvent.tool_result` forwards `artifacts` onto the SSE payload (single site; chat + run transports). No `loop.py` change beyond the existing constructor.

#### Added (persona-api)
- **`WorkspaceDirPersister`** (`services/workspace_persister.py`) — concrete adapter wrapping the `_persist_bytes` recipe (blake2b + `O_NOFOLLOW` + `.f5.json` sidecar), RLS-scoped to the persona owner; injected at `RuntimeFactory._build_toolbox`.
- F5 sidecar literals widened (`type="diagram"`, `producing_spec="28"`); uploads serve route serves the rich-output extensions (D-28-10 reuse + `_RICH_OUTPUT_MEDIA_BY_EXT`).

#### Added (persona-web)
- Inline **`FileCard`** + sliding **`FileRendererPanel`** (conversation-scoped, eye/`<>` toggle, Copy/refresh/close, Esc + Cmd/Ctrl+/) + **10 format renderers** (markdown, code, plaintext, JSON, CSV, PDF, image, HTML, Mermaid, Graphviz). New `file-card` `OutputContent` variant + normaliser; FileCard wired into the F4 dispatcher.
- **Security (D-28-X-svg-sanitization):** single `lib/sanitize.ts` (DOMPurify) sanitizes every SVG path (Mermaid + Graphviz); HTML = sandboxed iframe + DOMPurify; markdown = `rehype-sanitize`. Unit-tested (8 XSS vectors).
- **Deps:** `react-markdown` + `remark-gfm` + `rehype-sanitize`, `react-pdf`, `papaparse`, `react-json-view-lite`, `dompurify`, `mermaid` (lazy), `@hpcc-js/wasm-graphviz` (lazy). CSV uses a plain table (PapaParse); `@tanstack/react-table` evaluated and dropped (minimal-deps).

#### Notes
- The operator pass caught 4 integration bugs the unit gates missed (PDF worker resolution under Turbopack; chat dropped `artifacts`; `projectToolEvents` early-return for `operationFor==null` tools; serve route 404'ing text/diagram types) — all fixed; `e2e/spec28-rich-output.spec.ts` ships as the regression vehicle. A persona-web CSP is recorded as an app-hardening fast-follow.
### Persona, Runtime & Memory Integration for Voice (Spec V5; close-out 2026-06-14, pending sign-off)

> **The integration thread that makes the voice persona *the same persona*.** Fills V4's `ModelReplyProducer` seam with real persona-conditioned, tier-routed, streaming, cancellable generation, and writes voice turns to the **same** episodic store as text (unified memory). The binding constraint — *voice must never become a persona-bypass* — is enforced structurally: the voice turn composes the **shared** `PromptBuilder.build` + the **extracted** `retrieve_context` (never a thinner "voice prompt"). Operator pass **0 FAIL** across every V5 surface, live against real backends (S1 constraint refusal + S3/S9 real-memory recall) ([`operator_pass_2026_06_14.log`](docs/specs/phase2/spec_V5/evidence/operator_pass_2026_06_14.log)). `mypy --strict` voice (53) + runtime (35) + core (144) clean; `ruff` clean; 4378 unit + the V5 full-turn-cycle integration test pass. **Zero new external dependencies; one internal workspace edge (`persona-voice` → `persona-runtime`).**

#### Added (persona-voice)
- **`persona_voice.model`** — the persona-conditioned model side of the voice loop: `VoiceTurnContext` (session-bound DI container, fail-fast on a missing typed store), `VoicePromptAssembler` (D-V5-1 — caches the constant persona block once per session, retrieves the variable stores per turn, builds via the shared `PromptBuilder`), `VoiceRoutingPolicy` (D-V5-2 — a hard first-token-latency gate then best-quality-under-gate, layered on Spec 23's `IntelligentRouter`; degrades to rule-based slot-0), `VoiceModelReplyProducer` (fills the V4 seam: streaming spoken-text-only generation, `chunk.reasoning` never synthesised, first-token stamping; the conservative single voice tool round), `VoiceHistoryCompactor` (D-V5-3 — fast live-history view + off-critical-path background compaction), the voice-tools design (`VoiceToolPolicy` / `VoiceToolNarrator` / `run_tool_with_latency_bound` / `DeferredArtifact`, D-V5-4/5), and `VoiceTurnRecorder` (D-V5-X — unified voice→episodic write on commit only, barge-over-honest).
- **`persona-runtime` workspace dependency** added (the one structural edge; voice→runtime→core stays acyclic).

#### Added (persona-runtime)
- **`persona_runtime.retrieval.retrieve_context`** — the per-turn conditioning retrieval **extracted from `ConversationLoop._retrieve`** (D-V5-6) so the voice turn shares it verbatim (never reimplemented — the anti-bypass guarantee). The text loop now delegates to it, byte-identical; an added `identity=` keyword is the D-V5-1 session-cache hook.
- **`IntelligentRouter.select_model`** gains an additive, defaulted `candidate_filter` (the gate-then-score hook D-V5-2 passes the voice TTFT gate through). Byte-identical for the existing caller.

#### Operator-pass finding (recorded for fast-follow — see `MAINTENANCE.md`)
- Real model-slice first-token latency for the configured slot-0 model (NVIDIA nemotron) measured at **≈1.6–2.4 s across runs — ~2.7–4× over the ~600 ms voice gate**. The D-V5-2 gate fixes this when intelligent routing is enabled (it selects the fast small-tier model already configured, e.g. Groq `llama-3.1-8b`). Open fast-follow: should the voice TTFT gate apply **unconditionally** to voice turns rather than only under opt-in intelligent routing?

### Tools v2 — Tool Catalog Expansion + Persona-Driven Tool Selection (Spec 26; Phase 6 complete 2026-06-14, pending sign-off)

> **Two coupled deliverables:** (1) **7 new general-utility built-in tools** that personas previously fabricated via `code_execution`; and (2) **persona-driven tool selection** — an authoring-time recommender + a runtime tool-gap detector that offers one-tap, consent-gated tool enabling. Purely additive to Spec 04; existing personas are byte-for-byte unaffected (verified). **19 decisions** ([`docs/specs/phase2/spec_26/decisions.md`](docs/specs/phase2/spec_26/decisions.md)). Operator pass **12/12 live, 0 FAIL** ([`operator_pass_2026_06_14.log`](docs/specs/phase2/spec_26/evidence/operator_pass_2026_06_14.log)). `mypy --strict` core (120) + `mypy` runtime (30) / api (57) clean; `ruff` clean; 3296 unit + 16 spec-26 integration tests pass.

#### Added (persona-core)
- **7 built-in tools** (`persona/tools/builtin/`): `calculator` (hand-rolled AST-whitelist arithmetic + `math.*`, no `eval`, DoS-capped), `datetime` (timezone math via stdlib `zoneinfo`), `currency_convert` (Frankfurter no-key default + provider-conditional key guard), `regex_match` (RE2/`google-re2` — ReDoS-immune by construction, since the pattern is model-supplied), `json_query` (JMESPath), `text_diff` (stdlib `difflib`), and the runtime-wired `text_summarize`. Each returns `ToolResult(is_error=True)` on failure — never raises (D-03-5).
- **Known-tool catalog** (`persona/tools/catalog.py`, `TOOL_CATALOG`) — the single declarative vocabulary of every platform tool (incl. runtime-wired `code_execution`/`generate_image`/`text_summarize`); drives the recommender's catalog-validity filter + the runtime gap-detector's phrase→tool map. `warn_unknown_declared_tools` is soft-WARN only (no hard validation — backward-compat, D-26-X-known-tool-catalog).
- New domain exception `CalculatorError`.
- **Dependencies:** `jmespath>=1.0,<2` (pure-Python, zero transitive), `tzdata>=2024.1` (pure-data, cross-platform tz), `google-re2>=1.1,<2` (ReDoS-immune; cp312 `manylinux_2_28` x86_64 wheel — installs as a wheel in `python:3.12-slim`, no source build).

#### Added (persona-runtime)
- **`proactive_tool_gap.py`** — `detect_tool_gap` (post-generation; a capability-gap phrase + a catalog keyword for a tool NOT in the persona's allow-list) + `build_tool_gap_question` (Spec-21 3+1 consent offer). Wired into `ConversationLoop.turn` as a post-generation hook (Spec 21's pre-generation question hook untouched).
- **`TurnLog`** gains `tool_gap_detected` + `tool_consent_granted` (runtime-only JSONL; no migration). `turn()` gains an additive `consent_granted_tools` kwarg.

#### Added (persona-api)
- **Tool recommender** — `recommend_tools_for_persona` (`authoring_service`) + `POST /v1/personas/recommend-tools` (mid-tier, forced-JSON + catalog-filtered + confidence-floored + capped at 10). New `ToolRecommendation`/`ToolRecommendationResponse`.
- **Tool consent** — `tool_consent_service.grant_tool_consent` + `POST /v1/personas/{id}/tools`: adds the tool to the persona's allow-list (YAML column, no migration) and records a versioned `persona_self` self-fact (`force=True` + confidence ≥ 0.8 + reason). Idempotent; unknown tool → `ToolNotAllowedError`.
- `catalog_service.list_tools` now sources from the core `TOOL_CATALOG` so the new tools surface in authoring.

#### Changed
- **D-26-1:** `markdown_render` dropped from the launch set (no first-party HTML consumer; the model emits markdown natively). Reinstatement path recorded (`mistune` + mandatory `nh3` sanitizer).

### Spec 23 — Intelligent Routing: Cost/Quality/Latency-Aware Model Selection (Phase 4 complete; operator-pass green, pending final sign-off)

> **Opt-in, metadata-driven model selection WITHIN a tier.** The rule-based router still picks the tier (frontier/mid/small — ARCHITECTURE §5.3 / §9 intact); a new `IntelligentRouter` then scores the candidate models in that tier's MODELS list on cost / quality / latency (+ a hard capability gate) and picks the best, re-wrapping the tier backend so the chosen model is primary (Spec 20 fallback chain preserved). Deterministic scoring on **published metadata** — no router model, no embeddings (§9.10 editorial enrichment). Default **off**; existing personas route byte-identically (criterion 11, proven via a router-present-but-disabled contract test). Zero new dependencies.
>
> **Gates:** 12 acceptance criteria — see [`docs/specs/phase2/spec_23/closeout.md`](docs/specs/phase2/spec_23/closeout.md). `mypy --strict` core (122) + runtime (33) + `mypy` api (56) clean; `ruff` clean (577 files); **3243 unit tests passed, 25 skipped**, zero Spec 05/18/20/22 regressions. Operator pass pending (tool-touching: model-callable selection).

#### Added (persona-core)
- **`ModelMetadata`** + **`ModelMetadataResolver`** Protocol ([`backends/model_metadata.py`](packages/core/src/persona/backends/model_metadata.py)) — per-model cost (cents/1k, matching `TierMetadata`), normalised quality (`[0,1]`), published latency, capability flags, context length, `cost_verified_at_deploy`.
- **Static per-provider metadata tables** ([`backends/metadata/`](packages/core/src/persona/backends/metadata/)) — `anthropic / openai / google / deepseek / nvidia`, the single authoritative numbers home (D-23-X-metadata-placement); `StaticModelMetadataResolver`, `OpenRouterModelMetadataResolver` (wraps the Spec 22 catalog, fail-open), `ChainedModelMetadataResolver` (static-authoritative-on-overlap → OpenRouter-for-coverage).
- **`IntelligentRoutingError`** + **`BudgetExceededError`** ([`backends/errors.py`](packages/core/src/persona/backends/errors.py)) — wrapper-layer family (D-20-16 partition).
- **`routing.intelligent`** + **`routing.budget`** persona-YAML blocks ([`schema/persona.py`](packages/core/src/persona/schema/persona.py)) — additive, optional, **no `schema_version` bump** (D-23-9 dropped; D-01-12/`autonomy` precedent).

#### Added (persona-runtime)
- **`IntelligentRouter`** ([`routing/intelligent_router.py`](packages/runtime/src/persona_runtime/routing/intelligent_router.py)) — composes with (does not replace) the rule-based router; degrades to slot-0 on metadata miss (criterion 9); per-turn hard-cap fail-loud.
- **`model_scorer.py`** (capability pre-gate → normalised weighted-sum → lexicographic tie-break, deterministic) + **`routing_budget.py`** (pure evaluator: hard per-turn, soft per-session/per-day re-weighting) + **`model_selection.py`** (`reorder_primary` cheap re-wrap seam + `candidate_models_for` registry accessor).
- **`RoutingDecision`** extended additively with the model-selection audit trail (`model_candidates`, `score_vector`, `weights_used`, `model_fallback_engaged`, `model_fallback_reason`) — flows onto the JSONL `TurnLog` (criterion 10; runtime-only, no migration). `nvidia_models.py` reconciled to **derive** its `TierMetadata` from the core numbers home (no duplicated numbers).

#### Added (persona-api)
- **`RuntimeFactory`** wires one app-scoped `IntelligentRouter` (static metadata + OpenRouter when `PERSONA_OPENROUTER_API_KEY` is set) + a shared `FirstTokenLatencyTracker` into every per-request loop; per-persona `enabled` gates use.

#### Changed / notes
- **ARCHITECTURE.md §9.10** editorial enrichment (not a reopening): rule-based routing extended with deterministic metadata scoring; cites the in-tree Spec 18 precedent.
- **Per-day budget cap is not yet enforced** (no cross-session spend store) and **fails loud at startup** rather than silently no-op (D-23-X-per-day-fail-loud). Per-turn + per-session ship functional.

#### Fixed
- **OpenRouter resolver crash on negative sentinel pricing** (found by the operator pass, D-23-X-openrouter-negative-pricing) — the live catalog returns `"-1"` (variable/not-applicable) pricing on some entries, which violated `ModelMetadata`'s `ge=0` cost bound and crashed the resolver. The resolver now skips-and-WARNs entries that fail validation (mirrors the Spec 22 catalog-parser skip pattern); a skipped entry is a metadata miss → static fallback → rule-based.

### Skills v2 — Abstract Document Generation + Skills Ecosystem Maturation (Phase 5 complete 2026-06-14, pending sign-off)

> **Two coupled deliverables, one spec:** (1) the five document-format builtin skills (`docx`/`pdf`/`pptx`/`xlsx_generation` + `document_drafting`) collapse into one parameterized **`document_generation`** instruction-pack skill with registry-dispatched format handlers — the model still writes code in the `code_execution` sandbox, so persona-core takes **zero** new rendering dependencies; and (2) **skills-ecosystem maturation** — a richer `SKILL.md` schema, depth-capped skill composition, token-budget telemetry, and a lightweight `skills.toml` catalog. **13 decisions locked**; **zero new dependencies** (every rendering lib already ships in the sandbox image; `parameters` validation and the catalog reuse Pydantic + stdlib `tomllib`).
>
> **Backward compatibility is non-negotiable:** every persona YAML declaring a deleted skill name keeps working via an alias shim (INFO log per resolution, v0.3 WARN, v0.4 removal). The behavior tests (`test_use_skill_tool.py`, `test_tools_skills.py`) stay byte-for-byte; the structure tests' coverage was relocated onto `document_generation` + alias-resolution assertions. Default pytest **3614 passed**; `mypy --strict` core clean; `ruff` clean.

#### Added (persona-core)
- **Unified `document_generation` skill** ([`skills/builtin/document_generation/`](packages/core/src/persona/skills/builtin/document_generation/)) — one `SKILL.md` covering six formats (`docx`/`pdf`/`pptx`/`xlsx`/`md`/`txt`) with the migrated supplements (format-prefixed) + four placeholder templates. Dispatch **code** lives in [`skills/document_generation/`](packages/core/src/persona/skills/document_generation/): a `DocumentHandler` protocol + `FormatHandler` descriptors + a `registry` (format/template resolution; `UnknownDocumentFormatError` / `UnknownDocumentTemplateError`). New format = a handler module + registry entry, no new top-level skill.
- **Enhanced `SkillSpec` schema** — `parameters` (JSON Schema), `not_for`, `composes_with`, `output_format`, `token_budget`, parsed from the `SKILL.md` `metadata` block (Agent-Skills-standard escape hatch). Strict `parameters` validation at `use_skill` call time via a Pydantic model compiled from the schema (`skills/parameters.py`; `SkillArgumentValidationError`) — no `jsonschema` dependency.
- **Skill composition** ([`skills/composition.py`](packages/core/src/persona/skills/composition.py)) — depth-3 cap + visited-set cycle detection + a single shared token budget (`SkillCompositionState`; `SkillCompositionDepthError` / `SkillCycleError`). Budget exhaustion skips a composed skill whole (never truncates, never fails the turn).
- **`skills.toml` catalog** ([`skills/catalog.toml`](packages/core/src/persona/skills/catalog.toml) + `skills/catalog.py`) — declarative builtin index + named collections; a persona references `collection:<name>` / `skill:<id>` / a bare id. Local, zero-network precursor to the deferred federated registry (federation fields reserved-not-implemented; `SkillNameCollisionError` on a collection/skill name clash).
- **`code_review` builtin skill** — language-neutral review process with an untrusted-input security posture and a structured Critical/Suggestions/Verdict output (D-24-7). Summarisation folded into `web_research` (named in its `when_to_use`, not a standalone skill).
- **Alias shim** ([`skills/aliases.py`](packages/core/src/persona/skills/aliases.py)) — the 5 deleted skill names resolve to `document_generation` at scan time (dedup + INFO log).

#### Added (persona-runtime)
- **Composition discipline in both loops** — the `use_skill` intercept in `loop.py` + `agentic/loop.py` applies the shared depth/cycle/budget state (surgical; only the intercept changed).
- **TurnLog skill telemetry** — `skills_invoked` (full `SkillInvocation` records: name + params + injected size) + `skill_budget_exceeded`. Runtime-only JSONL fields; the Postgres writer maps a fixed columnar subset, so **no migration** (D-24-10).

#### Changed
- **Deleted** the 5 document-format skill directories (D-24-9); the catalog service surfaces the 4 live skill folders.
- **`docs/ARCHITECTURE.md` §4.5 + §9.3** editorial amendments (ecosystem-maturation paragraph; catalog-vs-federation boundary).

### Spec V4 — Turn-Taking, Interruption, and Full-Duplex Orchestration (persona-voice — Phase 5 complete 2026-06-14, pending sign-off)

> **The orchestration core of the voice loop** — what turns V1's transport, V2's transcripts, and V3's interruptible synthesis into a conversation. A four-state conversational machine (Listening / UserSpeaking / Processing / PersonaSpeaking) and the two judgement calls that make it feel alive: **automatic endpointing** (was that the end of the turn, or a mid-thought pause?) and **barge-in interruption** (the user spoke over the persona → yield the floor at once). It owns the full-loop latency number, the model-invocation turn cycle (invoke V5 → stream into V3 → cancel on barge-in), barged-over memory honesty, and a lean-conservative graceful-degradation bias. **11 decisions** ([`docs/specs/phase2/spec_V4/decisions.md`](docs/specs/phase2/spec_V4/decisions.md)); **zero new third-party dependencies** (pure-Python decision logic on V1/V2/V3 seams).
>
> **Gates at close:** 10 acceptance criteria — **8 ✅ MET** (state machine, endpointing, barge-in fast+discriminating, model cancellation, graceful degradation, mypy/ruff, deterministic unit + wired integration) + **2 🟦** carried to V5 (criterion #1 full end-to-end + criterion #9 live persona "feels natural" need the persona model). Operator pass (2026-06-14, Tier-A stub-backed): **9/9 scenarios PASS, zero FAIL**, rubric mean 4.7 (every-dim ≥3); D5 against budget-proxy TTFT + D7 mechanism, both 🟦-revalidated at V5 close. Default pytest **3973 passed, 26 skipped / 0 regressions**; voice unit + V4 integration 439 passed; `mypy --strict` clean (44 voice src); `ruff` clean. One V1 source file edited (additive opt-in); existing V1/V2/V3 tests byte-for-byte green.

#### Added (persona-voice — new `turn_taking/` sub-package)
- **`states.py`** — the conversational state machine: `ConversationalState` (the four states) + `AgentState`/`UserState` derived projections, `TransitionTrigger`, a trigger-driven `advance()` with guarded transitions (`InvalidConversationalTransitionError`), `is_legal_transition`, and the frozen `ConversationalTransition` hook record. Barge-in (`PERSONA_SPEAKING→USER_SPEAKING`) is legal; skipping `PROCESSING` is not.
- **`controller.py`** — `TurnTakingController.decide_turn_end` (pure, clock-injected): silence-duration threshold + provider-corroboration weighting + a **deterministic textual-completion gate** (the `DEFAULT_TURN_END_HOLD_TOKENS` hold-list — the endpointing analog of the backchannel list; buys mid-thought patience without a model, D-V4-1) + the conservative no-transcript bias (D-V4-6).
- **`barge_in.py`** — `BargeInDetector.decide_barge_in` (pure): confirm-window + Silero confidence/energy gate + duration-bar backchannel rejection (D-V4-2/3); `INTERRUPT`/`IGNORE`/`PENDING`.
- **`orchestrator.py`** — `ConversationalOrchestrator`: the `SpeechActivityListener` that drives the machine via an injected `Scheduler` (deterministic timers) + `clock`; runs the controller/detector at the right moments, performs turn actions through a `TurnActions` seam, broadcasts on a `ConversationalStateListener` (V6), owns the agent-speaking mute-window provider (`is_agent_speaking`, D-V2-X-echo-cancellation), exposes `last_endpoint_silence_wait_ms` + `force_reset()` recovery.
- **`bridge.py`** — `wire_orchestrated_loop` composition root + `LoopTurnActions` (cancellable model task + 2 s cancel **watchdog**, D-V4-X-watchdog-timeout) + `SessionEventBridge` (feeds user-side lifecycle events onto V1's existing `notify()` seam, no transition-logic change) + `CompositeStateListener` + `HeardWordsBridge`.
- **`heard_words.py`** — `BargedReply` (V5 memory-write record) + `TurnTranscriptListener` seam (D-V4-4 barged-over memory honesty: record what was *heard*, discard the unspoken remainder).
- **`latency.py`** — `attribute_hops` (per-hop breakdown over the existing `VoiceLog` anchors) + `compute_full_loop_ms`; **dual-line**: processing round-trip vs the 800 ms/1.5 s budget + a separate `endpoint_silence_wait_ms` so the threshold cost is never hidden (D-V4-X-eou-stamp-point).

#### Changed (persona-voice — V1 `loop/streaming.py`, additive opt-in)
- Additive `orchestrator=` + `turn_transcript_listener=` ports + properties; extracted public `invoke_model_for_turn(transcript)`; `start_pipeline` drains transcripts into the orchestrator and **never auto-invokes** when an orchestrator is wired (the auto-loop is the echo/dev baseline only — production always wires an orchestrator, D-V4-X-t05-orchestrator-default). New `HeardReply` record + `ReplyHeardListener` + `TurnOrchestrator` consumer-defined Protocols; `interrupt()` refactored to expose notify-free `flush_outbound_and_cancel_tts`. V1's contract + its tests are unchanged.

#### Notes
- **Operator-pass:** tool-touching (the audio loop is the tool surface) — Tier-A pass committed at [`evidence/operator_pass_2026_06_14.log`](docs/specs/phase2/spec_V4/evidence/operator_pass_2026_06_14.log) against [`operator_pass_charter.md`](docs/specs/phase2/spec_V4/evidence/operator_pass_charter.md); Tier-B (live persona feel) + the two 🟦 items (D5 absolute latency, D7 end-to-end memory honesty) inherit into the V5 operator pass per the 🟦 convention.
- **KNOWN-LIMITATION:** the heard-words counter over-counts by the buffered-but-unplayed tail on barge-in (`MAINTENANCE.md` Cluster C; fix = playout-position tracking, additive).
- **Chain numbering:** the additive amendment surfaces (the `orchestrator=`/`turn_transcript_listener=` ports, `turn_taking/` sub-package, the new listener/transcript seams, the dual-line latency field) defer to R-19-1 (no self-numbering).

### Spec 21 — Proactive Autonomy: Question Asking + Task Auto-Dispatch (Phase 6 complete 2026-06-13, pending sign-off)

> **Two coupled autonomy features, one spec:** (1) **proactive clarifying questions** (3 predefined options + 1 free-form) across chat *and* agentic-loop contexts, tuned by a new per-persona **autonomy preference** (`cautious | balanced | decisive`, YAML-default + `persona_self`-learnable); and (2) **consent-gated task auto-dispatch** — a request mapping to the persona's declared tools/skills can auto-start a Run, with a one-time per-persona consent gate. **20 decisions locked** (Phase 4) per [`docs/specs/phase2/spec_21/decisions.md`](docs/specs/phase2/spec_21/decisions.md); zero new dependencies.
>
> **Gates at close:** 10 acceptance criteria — **8 ✅ MET + 2 🟦 MECHANISM-MET** (consent gate live `post_message` SSE wiring + the web edit-page OpenAPI-client regen are named fast-follow wirings; all underlying mechanisms are unit + integration tested). Default pytest **3488 passed, 25 skipped / 0 Spec 05/06/09/19 regressions**; Spec 21 integration **11 passed, 1 skipped** (RLS skip without `persona_app`, D-07-5); web **644 vitest** + tsc clean; `mypy --strict` core (112) + `mypy` runtime/api (85) clean; `ruff` clean. All surfaces additive; existing personas/tests byte-for-byte unaffected.

#### Added (persona-core)
- **`Persona.autonomy`** ([`schema/persona.py`](packages/core/src/persona/schema/persona.py)) — `Literal["cautious","balanced","decisive"]`, default `"cautious"`; additive per the D-01-12 / `visual_style` precedent (existing YAMLs unaffected; resolved at load time, never mutated — D-21-11).
- **`persona.autonomy`** module — `AutonomyLevel`, `AmbiguityClass` (4 classes, defined in core for downward import), frozen `AutonomyPolicy` + per-level table (D-21-5 caps: cautious 5/run, balanced 3/run, decisive 1/run; class-D-always / class-C-never gating), `resolve_autonomy` (load-time self_facts overlay), and `record_autonomy_update` (persona_self force-write + stateless day/session cooldown D-21-4 + audit). New `InvalidAutonomyLevelError` / `AutonomyCooldownError`.

#### Added (persona-runtime)
- **`questions.py`** — frozen `QuestionOption`/`ProactiveQuestion` (exactly-3 validator, D-21-9), `QuestionRegistry` (sha256-normalized dedup + answer reuse, D-21-6), `validate_answer` boundary validation, `normalize_question`. New `InvalidQuestionAnswerError`.
- **`ambiguity.py`** — pure `detect_ambiguity` (4 classes, hard suppressors, EN + Norwegian Bokmål patterns, deictic referent gating, long-message windowing) + `should_ask` gating + `AmbiguityEscalator` tier-2 Protocol seam (D-21-1, unimplemented).
- **`question_author.py`** — `QuestionAuthor` port + deterministic `TemplateQuestionAuthor` (D-21-14 mandatory fallback; model-author is the injectable seam).
- **`task_detector.py`** — data-driven `TaskTriggerRegistry` (20-entry seed, constructor-injected, allow-set-filtered, `\b`-anchored regex, dual-knob scoring + guards, D-21-3); margin-tie → clarify.
- **Loop wiring:** `ConversationLoop.turn` gains a PRE-generation question decision point (D-05-12 ordering; ask → end turn, or stated-assumption nudge D-21-18); `AgenticLoop` `[ASK_USER]` gains 3+1 options + autonomy-scaled per-run cap + dedup (consumes a step, D-21-15). Additive `options`/`allow_free_form` on `RunEvent.asking_user` (absent = byte-identical back-compat).

#### Added (persona-api)
- **Migration `008_persona_consent_dispatch`** — tri-state `personas.consent_to_auto_dispatch BOOLEAN NULL` + `consent_updated_at TIMESTAMPTZ NULL` (D-21-7; `ADD COLUMN IF NOT EXISTS` per the 003/004 precedent).
- **`consent_service.py`** — pure tri-state machine (`can_auto_dispatch` / `should_prompt_for_consent`, D-21-17 stable-decline) + DB read/set (re-read per dispatch). **`PATCH /v1/personas/{id}/consent`** (grant/decline/revoke) + AuditEvent per transition; `PersonaDetail` carries the consent fields.
- **`dispatch_service.py`** — auto-dispatch trigger (D-21-10 layer split): pure `decide` truth table, `consent_question` (3+1, D-21-16), `parse_consent_answer` (modify = safe default), `detect_task` bridge, async `auto_dispatch`.

#### Added (persona-web)
- **`AutonomyConsentSection`** ([`components/persona/autonomy-consent-section.tsx`](packages/web/src/components/persona/autonomy-consent-section.tsx)) — autonomy selector (3 levels) + consent toggle with inline revocation warning (D-21-2: toggle + warning, no modal; off → revoke-to-ask).
- **3+1 question rendering** — `AskUserPrompt` renders 3 option buttons + free-form when present, free-text fallback when absent (D-21-9); `AskingUserData`/`RunStep` carry the additive `options`/`allow_free_form`.

#### Notes
- **Operator-pass:** EXEMPT (§6.3 — no model-callable tool/provider/sandbox/imagegen/voice surface added or rewired); recorded in [`closeout.md`](docs/specs/phase2/spec_21/closeout.md).
- **Chain numbering:** 11 additive-amendment surfaces deferred to R-19-1 (no self-numbering).
- **Fast-follow wirings:** live `post_message` consent-question SSE + answer-parse; web OpenAPI-client regen + edit-page wiring of `AutonomyConsentSection`.

### Spec V3 — Streaming Text-to-Speech + Per-Persona Voice (persona-voice 0.V3.0 — Phase 6 complete 2026-06-12, pending sign-off)

> **Two coupled deliverables, one spec:** (1) a provider-independent **`StreamingTTS`** Protocol + concrete **Cartesia Sonic 3.5** streaming backend (the outbound voice path's last hop: V5 reply text → V3 synthesis → V1 transport), and (2) **per-persona `voice` as a first-class identity attribute** with a cloning-seam resolution indirection (catalogue selection at v1; **cloning explicitly NOT implemented** — biometric-adjacent serious-harm surface). Mirrors V2's `stt/` subpackage verbatim (Spec 02 ChatBackend discipline). **16 decisions locked** (6 spec-standard D-V3-1..6 + 8 surfaced micros + 2 implementation-invariant) per [`docs/specs/phase2/spec_V3/decisions.md`](docs/specs/phase2/spec_V3/decisions.md).
>
> **🎯 Architectural bet VALIDATED at ~9 LOC** — V1 pre-built the V2→V5→V3 pipeline + barge-in path, so V3's seam adapter slotted in with **zero seam reshape**; the only V1 source delta is the D-V3-5 step-4 outbound-queue flush (**1 functional LOC** in `streaming.py` + ~8-LOC additive `VoiceRoom.clear_outbound()`). Far under the ≤21 budget; V1's `TTSStream` seam proven correctly shaped.
>
> **Headline gates:** 12 acceptance criteria — **10 ✅ MET-in-CI + 2 ✅/🟦 splits** (live prosody + live latency, operator-passed informally at T14 per CSA-3, since Spec 25's canonical gate post-dates V3 Phase 5; reconciles at R-19-1). Default pytest **3735 passed / 0 V3 regressions**; integration **216 passed** (6 V3 in-process, criterion-2 BINARY proven); external 5 gates skip cleanly without creds; `mypy --strict` clean (173 files); `ruff` clean. All surfaces additive; existing personas/tests byte-for-byte unaffected.

#### Added (persona-voice `tts/` subpackage)
- **`StreamingTTS` Protocol** ([`tts/protocol.py`](packages/voice/src/persona_voice/tts/protocol.py)) — `synthesize(text_stream, voice) -> AsyncIterator[AudioChunk]` (`def -> AsyncIterator` per D-02-5) + `cancel()` + `close()` + `provider_name`/`model_name`/`consumes_raw_text`. Verbatim Spec 02 / V2 mirror.
- **`CartesiaStreamingTTS`** ([`tts/cartesia_backend.py`](packages/voice/src/persona_voice/tts/cartesia_backend.py)) — Sonic 3.5 WebSocket *contexts* API (the ONLY module importing `cartesia`); native raw `pcm_s16le` @ 24 kHz; `max_buffer_delay_ms=0` (client chunker load-bearing); SDK-exception → `TTSError` mapping; idempotent cancel/close; `list_voices` (catalogue); cost estimate. ElevenLabs Flash v2.5 documented as the alternative behind the same seam (D-V3-1).
- **Rule-based sentence/clause chunker** ([`tts/chunking.py`](packages/voice/src/persona_voice/tts/chunking.py)) — first-chunk-shorter + lookahead guard + abbreviation/decimal/initial protection + flush-on-end/discard-on-cancel (D-V3-2 + D-V3-X-sentence-tokenizer; pysbd is the named falsification upgrade).
- **`PCM16Reframer` + `assert_rail_format`** ([`tts/audio.py`](packages/voice/src/persona_voice/tts/audio.py)) — deterministic, no-pacing (D-V3-X-no-pacing-t06); odd-byte carry; progressive first-frame ramp. All providers native 24 kHz → re-framing not transcoding (R-V3-4).
- **Voice resolution (the cloning seam)** ([`tts/voice_resolution.py`](packages/voice/src/persona_voice/tts/voice_resolution.py)) — fallible `resolve_voice() -> ResolvedVoice` (D-V3-X-cloning-seam-shape); `TTSVoiceNotFoundError` on unknown provider / catalogue miss / no-default. Cloning NOT implemented (reserved `consent`/`addressing` hooks only).
- **Voice catalogue + boundary types** — `VoiceCatalogue` Protocol + `normalize_gender` ([`tts/catalogue.py`](packages/voice/src/persona_voice/tts/catalogue.py)); `ResolvedVoice` / `VoiceCatalogueEntry` / `VoiceGender` ([`tts/types.py`](packages/voice/src/persona_voice/tts/types.py), frozen + `extra="forbid"`); `TTSError` hierarchy ([`tts/errors.py`](packages/voice/src/persona_voice/tts/errors.py), rooted at `PersonaError`); `StreamingTTSConfig` (`env_prefix="PERSONA_TTS_"`, `SecretStr`, D-V3-2 chunk knobs) + `load_streaming_tts` factory.
- **V1 `TTSStream` seam adapter** ([`tts/seam_adapter.py`](packages/voice/src/persona_voice/tts/seam_adapter.py)) — `V1TTSStreamSeamAdapter` (chunker + backend; iterator-sentinel cancel + generation-id guard; D-V3-5 steps 1-3) + `build_seam_adapter` composition root.
- **`EU AI Act Art. 50` provenance flag** — `ResolvedVoice.ai_generated=True` (D-V3-X-ai-provenance-flag; binds 2026-08-02, catalogue voices included).
- **4 additive `VoiceLog` TTS fields** ([`logging.py`](packages/voice/src/persona_voice/logging.py)) — `tts_text_first_at` / `tts_first_audio_at` / `tts_provider_cost_cents_per_minute` / `tts_total_cents` (D-V3-X-cost + D-05-9).
- **`PERSONA_TTS_*` env block** in `.env.example` + **MAINTENANCE.md Cluster C** (4 V3 operator-commitment rows).

#### Added (persona-core)
- **`voice: VoiceSpec | None` on `PersonaIdentity`** ([`schema/persona.py`](packages/core/src/persona/schema/persona.py)) — additive (D-01-12; existing personas byte-for-byte unaffected, criterion 4); `CatalogueVoice` / `VoiceSpec` with the `"provider:voice_id"` string shorthand, the `kind` discriminator pre-positioned for v0.2 cloning, and the reserved always-`None` `consent` hook. Re-exported from `persona.schema`.

#### Changed (Spec V1 — additive, the architectural bet)
- **`VoiceRoom.clear_outbound()`** ([`transport/room.py`](packages/voice/src/persona_voice/transport/room.py)) — additive `rtc.AudioSource.clear_queue()` wrapper (~8 LOC); and **1 functional line** in `StreamingLoop.interrupt()` ([`loop/streaming.py`](packages/voice/src/persona_voice/loop/streaming.py)) calling it for D-V3-5 step-4 barge-in flush. V1's 31 existing streaming/room tests pass byte-for-byte.

#### Dependencies
- **`cartesia[websockets]>=3,<4`** (Apache-2.0; v3.2.0; ships py.typed — no mypy override needed, D-V3-X-mypy-tts-sdk-override resolved). Transitive surface (anyio / distro / httpx / pydantic / sniffio / typing-extensions / websockets) all permissive, mostly already in-workspace via deepgram/livekit.

### Spec 25 — Tool UX + Sandbox Reliability Hardening + Operator-Pass Acceptance Gate (Phase 6 complete 2026-06-13)

> **Three coupled deliverables, one spec** — all produced by the same production-reality gap the 2026-06-10 operator-pass surfaced: (1) **ten tool-surface fixes**, (2) **`CloudflareImageBackend`** (the truly-free image-gen path; NVIDIA free tier has no text-to-image — §2.10), and (3) the **operator-pass canonical close-out gate** in `SPEC_IMPLEMENTATION_PROMPT.md` §6.3 that all downstream tool-touching specs inherit. **22 decisions locked** (D-25-1..14 numbered + 8 `D-25-X-*` named) per [`docs/specs/phase2/spec_25/decisions.md`](docs/specs/phase2/spec_25/decisions.md). The gate applied to itself: [`evidence/operator_pass_2026_06_12.log`](docs/specs/phase2/spec_25/evidence/operator_pass_2026_06_12.log) = **10 PASS · 1 KNOWN-LIMITATION · 0 FAIL**.
>
> **Headline gates (scoped per the multi-session shared-checkout discipline):** consolidated Spec-25 tests = 253 passed / 20 skipped (T07 unlocked pkgs) / 3 deselected (web_fetch external); Spec-25 integration (`-m integration`) = 11 passed; web_fetch live `-m external` = 3 passed; `mypy --strict` (core) + standard (runtime/api) clean on touched files; `ruff check` + `format --check` clean. Backward compat: all surfaces additive; existing configs unaffected.

#### Added
- **`CloudflareImageBackend`** ([`packages/core/src/persona/imagegen/cloudflare_image.py`](packages/core/src/persona/imagegen/cloudflare_image.py)) — Workers AI text-to-image; content-type-branched decode (flux JSON-base64 / SDXL binary PNG); error-code→domain mapping; single-image posture (count>1 → `unsupported_option`). Allow-set: flux-1-schnell (GA, primary) + SDXL-base + dreamshaper-8-lcm (D-25-11).
- **Cloudflare wiring** — `ImageProvider` Literal `+cloudflare`, `DEFAULT_BASE_URLS`, separate `cloudflare_account_id` config field (D-25-12, NOT base_url-embedded), factory dispatch, `.env.example` default-recommended block (D-25-13). Added alongside the concurrent OpenRouter work without overwrite.
- **TurnLog telemetry fields** ([`packages/runtime/src/persona_runtime/logging.py`](packages/runtime/src/persona_runtime/logging.py)) — `cost_basis`, `fallback_rate_alert`, `tool_refusal_detected`, `refusal_retry_engaged`, `sandbox_session_recreated` (all additive; D-18-1 not reopened).
- **NVIDIA price-table entries** + `cost_basis_for()` + `nvidia/` catalog-prefix normalization (D-25-7; the §2.6 silent-miss fix).
- **Refusal observability + (default-OFF) auto-retry** — `detect_tool_refusals()` + `PERSONA_REFUSAL_RETRY_ENABLED` guardrail (T11/T21).
- **`SPEC_IMPLEMENTATION_PROMPT.md` §6.3 operator-pass gate** + **MAINTENANCE.md Cluster F** (7 operator-commitment rows).

#### Changed / Fixed
- **Sandbox image** ([`packages/core/src/persona/sandbox/image/`](packages/core/src/persona/sandbox/image/)) — full 31-package sci-Python stack (3 drops: plotly/opencv/toml; ≤500 MB build-gate).
- **Dual wall-clock policy** (30s exec / 120s env-setup, env-tunable) + **session auto-recovery** (retry-once on `no_session`) in the sandbox path — now also covers the **E2B idle-reap variant** (operator-pass 2026-06-13): a server-side-reaped sandbox ("sandbox not found"/502) is evicted + re-surfaced as `no_session` so the wrapper auto-recovers instead of the model retrying the dead sandbox (D-25-X-emergent-e2b-reap-recovery).
- **`web_fetch`** — descriptive default User-Agent (fixes Wikimedia 403) + empty-extraction UX message.
- **Sandbox path-hint UX** — all 7 `SandboxViolationError` raise sites carry a valid relative-path example.
- **multi_model fallback-rate alert** — rolling 10-turn window in the runtime turn loop (>30% → ERROR + `fallback_rate_alert`); **D-20-9 classifier unchanged** (R-25-1: rate-limit, not mis-categorization).
- **API `_compose_image_backend`** — now reads `PERSONA_IMAGEGEN_MODELS` (D-20-17 four-case parser); fixed a Cloudflare `account_id` env-namespace bug found mid-operator-pass (now accepts `PERSONA_CLOUDFLARE_ACCOUNT_ID`).
- **§2.9 generate_image hotfix verified** — `RuntimeFactory` wires `make_generate_image_tool` (the tool is now persona-callable; 6/6 integration tests).
- **D-13-3 reframed** ("estimate + flag", not "skip cost"); **D-20-1 footnoted** (catalog-vs-vendor naming + NVIDIA imagegen Enterprise-only). Editorial, no reopen.

#### Known limitation (named follow-up — deferred to an operator-authored rich-output rendering spec)
- **Chat-path rich-output delivery** (image / file / diagram inline render): persona-driven `generate_image` dispatches + produces bytes, but the chat-tool path has no bytes-persister, so the image isn't served/rendered inline (the HTTP `/v1/imagegen` path persists correctly). Named in `decisions.md` D-25-X-emergent-rich-output-delivery-deferred + MAINTENANCE.md Cluster F.

### Spec 22 — OpenRouter Integration + Auto-Detected Subscription Mode (Phase 6 complete 2026-06-11)

> **Two coupled deliverables, one spec:** (1) OpenRouter as a first-class Persona provider across chat / reasoning / vision / image-gen — 300+ aggregated models behind one OpenAI-compatible surface at `https://openrouter.ai/api/v1/`, slotting into Spec 20's `MultiModelChatBackend` / `MultiModelImageBackend` cross-provider fallback **unchanged** (native `<provider>/<model>` slash names match D-20-13 exactly). (2) Auto-detected free/paid subscription mode resolved once at startup via `GET /api/v1/key` (`is_free_tier`) — free-mode drops non-`:free` chat entries (D-22-2) and all OpenRouter image entries (D-22-20); paid-mode opens the full catalog. **20 production-merit decisions locked at Phase 4** (D-22-1..20: 11 spec/Phase-1-queued + 9 research-emergent) + 9 spec-body folds per [`docs/specs/phase2/spec_22/decisions.md`](docs/specs/phase2/spec_22/decisions.md). Phase 3 research (4 parallel workflows, live-verified against the public catalog) overturned three spec leans: probe is `/api/v1/key` not `/credits` (management-key gate, D-22-3); `:nitro`/`:floor` are dynamic routing transforms not separate models (D-22-6); image-gen rides chat-completions with no DALL-E (new `OpenRouterImageBackend`, D-22-8). **Additive-precedent chain entries** (D-22-1..20) claimed; R-19-1 canonicalizes at next audit per [`closeout.md §5`](docs/specs/phase2/spec_22/closeout.md).
>
> **Headline gates:** `pytest packages/core/ packages/runtime/` = 2964 passed / 26 skipped / 176 deselected; OpenRouter cross-spec integration = 11 passed (`packages/api/tests/integration/test_openrouter_integration.py`); `mypy --strict` on persona-core (112 files) + persona-runtime (resolver + tier) clean; standard `mypy` on persona-api `app.py` clean; `ruff check` + `ruff format --check` clean across all touched files. Backward compat: OpenRouter is opt-in (no key → unused); all existing Spec-20 configurations pass unchanged.

#### Added — Spec 02 (chat backends; OpenRouter provider surface)

- **OpenRouter `Provider` Literal entry** at [`packages/core/src/persona/backends/config.py:22-32`](packages/core/src/persona/backends/config.py) — `Provider` Literal extended to 9 entries (… / nvidia / **openrouter** / ollama / local).
- **OpenRouter `DEFAULT_BASE_URLS` entry** at `config.py` — `"openrouter": "https://openrouter.ai/api/v1/"` (the openai SDK appends `/chat/completions`; `/v1/` suffix kept per the openai-compat convention).
- **`OpenAICompatibleBackend` allow-set extension** + **`backends/_factory.py` `_OPENAI_COMPAT_PROVIDERS` extension** per the D-20-X-nvidia-allow-set-extend invariant — Spec 22 confirms it is a **FIVE-touch** (Provider Literal + DEFAULT_BASE_URLS + 2 capability matrices + in-`__init__` allow-set + factory-dispatch allow-set); the T15 integration test caught the factory-allow-set omission before commit (same class of gap as Spec 20's production-startup catch).
- **OpenRouter capability matrix rows + three-tier inference** at `openai_compat.py` (`_NATIVE_TOOLS_CAPABILITY` + `_VISION_CAPABILITY`) — empty operator-override rows (D-22-10f) plus the tier-1 (`_explicit_openrouter_entry`) / tier-3 (`_infer_openrouter_capability`) resolver: suffix taxonomy (D-22-6), author-prefix→provider map, dual match key, `:free` asymmetric conservatism (tools→False / vision→base, D-22-10c). Catalog metadata (tier-2) is the Spec-23 metadata source, not wired into per-construction resolution in v0.1 (YAGNI; AC9 met by tier-1+tier-3).
- **2 error classes** at `backends/errors.py` — `OpenRouterCatalogError` + `OpenRouterBalanceProbeError`, both under `ProviderError` (live-HTTP, D-20-16 partition); 401 reuses `AuthenticationError` (D-22-9, no `OpenRouterAuthError`).
- **`OpenRouterCatalogClient` + subscription state** at new `backends/openrouter_catalog.py` — sync `httpx` (D-22-11), `list_models()` (in-process cache D-22-5; `~`-alias filter + per-entry skip-WARN; D-22-14) + `get_key_info()` (the D-22-3 probe). Frozen response models with `extra="ignore"` (documented deviation, D-22-12) + Decimal-from-string pricing (D-22-13). `OpenRouterModelEntry.is_free`/`.supports_tools`/`.supports_vision` capability props (D-22-10b). `OpenRouterSubscriptionState` (our frozen `extra="forbid"` boundary type) + pure mappers `subscription_state_from_key_info` / `free_mode_fallback`. **Public read surface pinned by a stability contract test for Spec 23.**
- **`filter_openrouter_free_mode`** at `backends/credentials.py` — shared pure helper (mode injected as a string → persona-core stays free of a persona-runtime dependency); `keep_free_suffix` selects the chat (D-22-2, keep `:free`) vs image (D-22-20, drop all) posture.

#### Added — Spec 05 (TierRegistry; chat free-mode filter)

- **`tier_registry_from_env(openrouter_subscription_mode=...)`** at [`packages/runtime/src/persona_runtime/tier.py`](packages/runtime/src/persona_runtime/tier.py) — in free-mode, drops non-`:free` `openrouter/X` entries per tier with a WARN (D-22-2); a tier whose MODELS list empties is left unregistered (fail-soft → registry fallback chain). `None` default = no-op (full backward compat).
- **`resolve_openrouter_subscription`** at new `persona_runtime/openrouter_subscription.py` — startup resolver: no key → `None` (zero-touch); `PERSONA_OPENROUTER_SUBSCRIPTION_MODE` env override skips the probe (D-22-7); probe `OpenRouterBalanceProbeError` → conservative free-mode fallback (D-22-3); `AuthenticationError` → fail-loud propagate (D-22-9); client closed in `finally`.

#### Added — Spec 15 (image generation; OpenRouter image surface)

- **`OpenRouterImageBackend`** at new `imagegen/openrouter_image.py` — image-gen rides `POST /chat/completions` with `extra_body={"modalities": ["image","text"], "image_config": {...}}`; base64 data-URL unpack from the untyped `message.images` extra (D-22-8). `ImageGenOptions → image_config` nearest-aspect-ratio coercion + `count>1` raise (D-22-19); 403-moderation disambiguation → `ContentRejectedError` (D-22-16); text residue discarded. Reuses the existing `ImageBackend` protocol / options / errors / multi-model fallback unchanged.
- **OpenRouter `ImageProvider` Literal + factory dispatch** at `imagegen/config.py` + `imagegen/_factory.py` — `load_image_backend_from_env(openrouter_subscription_mode=...)` drops ALL `openrouter/X` image entries in free-mode (D-22-20: zero `:free` image-output models exist; fail-fast over a call-time 402). Acceptance criterion #2 model corrected to `openrouter/google/gemini-2.5-flash-image` (DALL-E does not exist on OpenRouter).

#### Added — Spec 08 (composition root; subscription wiring)

- **OpenRouter subscription resolution wired at startup** in [`packages/api/src/persona_api/app.py`](packages/api/src/persona_api/app.py) — `_resolve_openrouter_subscription_mode()` runs the probe once and threads the mode into both `_compose_image_backend()` (D-22-20) and `tier_registry_from_env()` (D-22-2). Composition-root degradation: a probe `AuthenticationError` is ERROR-logged and swallowed so one optional provider's bad key does not block API startup (consistent with the image-backend / E2B-less-pool graceful-absence pattern).

#### Added — `.env.example` (operator surface)

- **OpenRouter block** — `PERSONA_OPENROUTER_API_KEY` + optional `PERSONA_OPENROUTER_BASE_URL` + `PERSONA_OPENROUTER_SUBSCRIPTION_MODE` override + verified-2026 `:free` example ids (Nemotron-3 / gpt-oss / Gemma-4 / Qwen3-Next) with the 20 RPM / 50-or-1000 RPD / no-SLA operator note + a paid-mode image-gen example.

#### Cross-spec editorial (additive; no closed-spec re-open)

- **Spec 20** — OpenRouter slots into `MultiModelChatBackend` / `MultiModelImageBackend` as an ordinary provider (no wrapper change); the cross-provider-extension MAINTENANCE row is amended to a FIVE-touch invariant (the `_factory.py` allow-set). **Spec 13** — OpenRouter vision inherits underlying-model capability via the tier-3 inference. **Spec 15** — `OpenRouterImageBackend` is a NEW adapter (not the editorial base_url-reuse the spec assumed).

#### Operator commitments added to MAINTENANCE.md (T16, Cluster B; 12 → 15 rows)

- Subscription-probe operator awareness + `/credits` management-key open question (D-22-3); catalog + `:free`-roster + capability-matrix staleness (D-22-1 / D-22-10); free-tier daily-cap degradation (D-22-2 / D-22-17).

### Spec 20 — NVIDIA Provider Integration + Cross-Provider Multi-Model-Per-Tier Fallback (Phase 6 complete 2026-06-10)

> **Two coupled deliverables, one spec:** (1) NVIDIA as a first-class Persona provider across chat (Nemotron family) / reasoning (`enable_thinking` + `delta.reasoning_content`) / vision (NVIDIA VILA + Cosmos VLMs) / image-gen (FLUX.2-klein-4b via OpenAI-compat + SDXL via legacy GenAI) — all behind one `OpenAICompatibleBackend` adapter at `https://integrate.api.nvidia.com/v1/`. (2) Cross-provider multi-model-per-tier fallback (`PERSONA_<TIER>_MODELS=<provider>/<model>,<provider>/<model>,...`) across all text tiers AND image generation via `MultiModelChatBackend` + `MultiModelImageBackend` wrappers + per-provider `ProviderCredentialResolver`. **Co-shipped because NVIDIA's free-tier 40 RPM cap makes cross-provider fallback a v0.1 production-resilience prerequisite, not a v0.2 nicety.** 25 production-merit decisions locked at Phase 4 (12 LOCK + 5 emergent micros + 7 research-confirmed defaults + 1 closeout-editorial-only) per [`docs/specs/phase2/spec_20/decisions.md`](docs/specs/phase2/spec_20/decisions.md). **Test growth:** baseline 2643 → 2972 default + 44 conditional (40 integration + 4 external 🟦 operator-pass per CSA-3) = +329 default tests. **9 additive-precedent chain entries** claimed (T09-T17; anticipated chain ~23-31; R-19-1 canonicalizes at next audit per [`closeout.md §7`](docs/specs/phase2/spec_20/closeout.md)).
>
> **Headline gates:** 2972 default pytest passed / 3 skipped / 406 deselected; mypy --strict on persona-core (111 files) + persona-runtime (26 files) clean; standard mypy on persona-api (56 files) clean; `ruff check + ruff format --check` clean across all touched files.

#### Added — Spec 02 (chat backends; boundary types)

- **NVIDIA Provider Literal entry** at [`packages/core/src/persona/backends/config.py:22-30`](packages/core/src/persona/backends/config.py) — `Provider` Literal extended to 8 entries (anthropic / openai / deepseek / groq / together / ollama / local / **nvidia**).
- **NVIDIA DEFAULT_BASE_URLS entry** at `config.py:36-46` — `"nvidia": "https://integrate.api.nvidia.com/v1/"` (now 7 entries; `local` intentionally omitted per 7-vs-6 asymmetry; in-process HF, not network-resolvable).
- **OpenAICompatibleBackend allow-set extension** at `openai_compat.py:162-168` per D-20-X-nvidia-allow-set-extend atomic-four-touch invariant (Provider Literal + DEFAULT_BASE_URLS + capability matrices + allow-set MUST land together).
- **NVIDIA capability matrix rows** at `openai_compat.py:72` (`_NATIVE_TOOLS_CAPABILITY`) + `:106` (`_VISION_CAPABILITY`) — Nemotron 49b-v1.5 / 120b-a12b / Nano-Omni-30b chat + tool capability; Nemotron Nano-Omni-30b + VILA + Cosmos Nemotron 34b + Cosmos Reason 1-7b/2-8b vision capability (NVIDIA Open Model License — no EU carve-out per R-20-5).
- **StreamChunk.reasoning: str | list[ReasoningBlock] | None** boundary additive at `types.py:135` per T01 verdict (b) + R-20-2 multi-provider soak. NVIDIA / OpenAI Chat Completions / DeepSeek-R1 fit `str` arm; Anthropic emits LIST of typed content blocks (`ThinkingBlock.signature` cryptographic HMAC MUST round-trip; `RedactedThinkingBlock.data` opaque encrypted blob; `display="omitted"` signature-only blocks) requiring richer type. New `ReasoningBlock` frozen Pydantic class with `kind ∈ {thinking, redacted_thinking, summary, text}` + per-provider field semantics; helper `reasoning_as_text(r) -> str | None` collapses list arm for str-only consumers (prompt builder, audit logger, UI rendering).
- **BackendConfig.extra_body: dict[str, Any] | None** passthrough at `config.py:78-88` per D-20-3 — vendor-specific request-body extensions (e.g., NVIDIA `{"chat_template_kwargs": {"enable_thinking": True}, "reasoning_budget": N}`); Persona's backend layer does NOT validate dict contents.
- **OpenAI-compat stream-loop dual-probe** in `openai_compat.py` per D-20-X-nemotron-field-name-dual-probe — probes both `getattr(delta, "reasoning_content", None)` AND `getattr(delta, "reasoning", None)` (NVIDIA Nemotron canonical vs Nano-Omni VLM alias); both arrive via Pydantic extras on openai-py ChoiceDelta (NOT statically typed). T20 + T22 verify live behavior.
- **MultiModelChatBackend wrapper** at new `backends/multi_model.py` (477 src) implementing ChatBackend Protocol verbatim + D-20-9 three-bucket classifier (RETRY-THEN-FALLBACK / FALLBACK-NO-RETRY / SURFACE) + D-20-10 N=1 same-model retry with 200ms ±50% jitter + D-20-12 SKIP-AND-FALLBACK on cross-provider AuthenticationError with structured WARNING + D-20-15 runtime ProviderCredentialMissingError handling + two-phase streaming first-chunk fallback boundary (pre-first-chunk errors → classifier; post-first-chunk errors → raise verbatim preserving partial output).
- **ProviderCredentialResolver** at new `backends/credentials.py` (391 src) — single source of truth for resolving `<provider>` reference to `(api_key, base_url)` tuple via `PERSONA_<PROVIDER>_API_KEY` + `PERSONA_<PROVIDER>_BASE_URL` env vars. D-20-13 SLASH `<provider>/<model>` format; D-20-17 four-case precedence (a/b/c/d); D-20-18 EXPLICIT REJECT for `local` and `ollama` (HTTP-transport-shaped MODELS list can't compose in-process HF backend).
- **6 new error classes** at `backends/errors.py` — `AllModelsFailedError(PersonaError)` + `ProviderCredentialMissingError(PersonaError)` + `LocalProviderInModelsListError(PersonaError)` + `MalformedTierModelsError(PersonaError)` + `IncompleteTierConfigError(PersonaError)` + `TierNotConfiguredError(PersonaError)` — all root at `PersonaError` directly per D-20-16 settled partition (NOT under `ProviderError`). T18 cemented via 36 parametrized contract tests.
- **D-20-X-tier-name-backends-property-readers** Protocol-shaped accessor pattern — wrapper classes expose read-only `tier_name` + `backends` + `last_attempts` `@property` accessors. Reusable for future wrapper specs.

#### Added — Spec 05 (TierRegistry + ConversationLoop)

- **TierConfig.preconstructed_backend** cache field per option-(a) TierConfig injection at T17 — TierRegistry pre-seeds `_cache` for tiers carrying a pre-built MultiModel wrapper; `.get()` bypasses `load_backend` when present.
- **`tier_registry_from_env` D-20-17 four-case precedence** at `tier.py:358-405` — MODELS-only / triplet-only / both → MODELS wins + INFO log naming ignored triplet vars / malformed → fail-loud at construction (MalformedTierModelsError + IncompleteTierConfigError + TierNotConfiguredError). Backward-compat single-model triplet path preserved unchanged (acceptance 5d byte-for-byte).
- **TurnLog 5+1 fallback fields** at `runtime/logging.py:47` per T19 — `tier_model_chosen: str | None` + `tier_provider_used: str | None` + `tier_fallback_count: int` + `tier_fallback_reasons: list[str]` (class-names-only per D-15-X-hard-line-filter privacy mirror) + `tier_fallback_providers: list[str]` + derived `fallback_engaged: bool`. `model_validator` enforces length-match + bool-derived consistency invariants.
- **`_compute_fallback_fields` helper** at `runtime/loop.py:670+` reads MultiModelChatBackend.last_attempts and populates TurnLog at write-back. Single-backend (bare) callers safely return zero-fallback shape via `getattr(backend, "last_attempts", None) or []`.
- **TurnLog reasoning capture** at `logging.py` — `reasoning_total_tokens: int | None` + `reasoning_text_hash: str | None` (sha256, content-hash-only per D-15-X-hard-line-filter mirror) per D-20-5. Raw reasoning text NEVER persisted at v0.1.
- **D-20-X-deepseek-reasoning-strip-invariant** at conversation-history serializer — strips `reasoning_content` from prior assistant turns when active provider is DeepSeek (HTTP 400 otherwise).

#### Added — Spec 13 (vision capability matrix)

- **NVIDIA vision rows** at `openai_compat.py:106 _VISION_CAPABILITY` — `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` (T09 omni-modal entry) + `nvidia/vila` + `nvidia/cosmos-nemotron-34b` + `nvidia/cosmos-reason1-7b` + `nvidia/cosmos-reason2-8b` (T13 NVIDIA-owned VLMs preferred over Llama-3.2-Vision per R-20-5 EU carve-out for Norway/EEA context).
- **`_NVIDIA_VISION_MODELS_VERIFY_AT_DEPLOY`** Final companion constant per D-13-3 verify-at-deploy precedent. T25 MAINTENANCE.md Cluster B row tracks operator re-verification cadence per NVIDIA Jan-2025 VILA → Cosmos Nemotron rebranding drift signal.
- Spec 13 image-as-ref-or-base64 contract UNCHANGED — NVIDIA VLMs slot in via existing OpenAI-compat serializer.

#### Added — Spec 15 (image generation + safety)

- **ImageProvider Literal +"nvidia"** at `imagegen/config.py:29` (now 3 entries: openai / fal / nvidia).
- **NvidiaImageBackend** at new `imagegen/nvidia_image.py` (562 src; D-20-X-t10-loc-overshoot-accepted per soft-ceiling judgment-call — dual-branch surface + NVCF poll loop + license-block guards genuinely additive) per D-20-4 HYBRID dual-branch design: Branch B (OpenAI-compat preferred path) for `qwen-image` / `qwen-image-2512` / `flux.2-klein-4b`; Branch A (legacy GenAI custom body + NVCF async poll on HTTP 202) for `stabilityai/stable-diffusion-xl`.
- **D-20-X-flux-1-dev-license-block** guard — NvidiaImageBackend construction with `nvidia/black-forest-labs/flux.1-dev` OR `nvidia/black-forest-labs/flux.1-kontext-dev` raises `ImageProviderError(reason="non_commercial_license", hint="use nvidia/flux.2-klein-4b instead...")` per FLUX.1 [dev] Non-Commercial License (R-20-5 license-stack).
- **MultiModelImageBackend wrapper** at new `imagegen/multi_model_image.py` (491 src) mirroring MultiModelChatBackend shape + D-20-9 ContentRejectedError SURFACE invariant (CRITICAL Spec 15 invariant — `ContentRejectedError` NEVER falls back to secondary; would launder content-policy violations across vendors) + D-20-14 atomic generate (DISCARD+RESTART; NVIDIA NIM is one-shot HTTP POST with no partial state recoverable).
- **D-20-X-multi-model-image-edit-not-implemented** Protocol compliance shim — `MultiModelImageBackend.edit()` raises `NotImplementedError` per D-15-X-edit-protocol-reservation (no v1 backend overrides edit; nothing to fall back across).
- **`load_image_backend_from_env`** factory + `_parse_image_models_list` at `imagegen/_factory.py` — narrows T11's ProviderCredentialResolver parser to `_IMAGE_PROVIDERS={openai, fal, nvidia}` since `fal` isn't in chat-side Provider Literal but IS in ImageProvider Literal.

#### Added — Spec 18 (router metadata)

- **NVIDIA TierMetadata entries** at new `runtime/routing/nvidia_models.py` (110 LOC static D-20-1 launch-set registry) — Nemotron 49b-v1.5 chat-primary (reasoning_capable=False) + Nemotron 120b-a12b long-context (reasoning_capable=True) + Nemotron Nano-Omni-30b reasoning+vision.
- **TierMetadata.reasoning_capable: bool = False** additive field at `tier.py:78` — preserves existing TierMetadata constructions byte-for-byte.
- **TierMetadata.cost_verified_at_deploy: bool = True** additive field at `tier.py:78` per D-13-3 verify-at-deploy precedent; NVIDIA entries set False (R-20-4 confirmed NVIDIA does NOT publish $/Mtok per the hosted-catalog ToS).
- **D-18-5 quality-proxy boost integration** at `routing/scoring.score_tier` — +0.10 quality_fit when reasoning_capable=True AND quality_proxy >= 0.5. Below threshold neutral so routine traffic stays cost-sensitive.
- **`tier_metadata_from_env` extension** at `tier.py:337-405` honors `PERSONA_<TIER>_REASONING_CAPABLE` + `PERSONA_<TIER>_COST_VERIFIED_AT_DEPLOY` env vars with `{true,1,yes,on}` truthy parsing.
- **D-18-1 internal-heuristic scorer choice** NOT REOPENED — Spec 20 only extends scorer's input data; D-20-20 FALSE-TENSION reclassification per Phase 1 reviewer panel (credential resolution at construction-time ≠ scoring at turn-time).

#### Operator commitments added to MAINTENANCE.md (T25, Cluster B; 7 → 12 rows)

5 event-driven rows per acceptance criterion 12: NVIDIA hosted-catalog 40 RPM headroom monitoring (D-20-7 RESHAPED — event-driven NOT calendar-bound per R-20-4 trial-tier reality) + per-NVIDIA-model rate-limit monitoring + capability-matrix freshness review (T13 VILA/Cosmos verify-at-deploy) + multi-model fallback-rate monitoring (TurnLog `fallback_engaged` aggregate; threshold 10%) + cross-provider credential-resolver staleness (D-20-X-nvidia-allow-set-extend atomic-four-touch invariant for future provider additions).

#### Operator-pass surface (acceptance criterion 10 🟦 CSA-3)

T22 external smoke at [`packages/core/tests/external/test_nvidia_smoke.py`](packages/core/tests/external/test_nvidia_smoke.py) — 4 surface scaffolds (chat / reasoning / vision / image-gen Branch B) gated on `PERSONA_NVIDIA_API_KEY`; module-level skip cleanly without key. Operator runbook at [`docs/specs/phase2/spec_20/closeout.md`](docs/specs/phase2/spec_20/closeout.md) §"External smoke"; operator-override env vars `PERSONA_NVIDIA_SMOKE_<surface>_MODEL` per D-13-3 verify-at-deploy precedent. Approximate cost per run: low single-digit cents on paid tier; effectively free on 40 RPM trial; wall-clock <60s.

#### 11 named follow-ups (per [`closeout.md §6`](docs/specs/phase2/spec_20/closeout.md))

1. D-20-X-imagegen-audit-fallback-fields-followup (T19 deferred image-gen audit-log plumbing)
2. D-20-X-t10-loc-overshoot-accepted (NvidiaImageBackend 562 LOC soft-ceiling judgment-call)
3. D-20-X-t20-loc-overshoot-accepted (cross-spec integration test 616 LOC documentation-discipline)
4. D-20-X-t21-loc-overshoot-accepted (cross-provider fallback test 926 LOC documentation-discipline)
5. D-20-X-t22-loc-overshoot-accepted (external smoke scaffold 324 LOC documentation-discipline)
6. D-20-X-worktree-isolation-harness-anomaly (operational note; 69% leak rate observed; routed to harness-optimizer)
7. D-20-X-tier-name-backends-property-readers (reusable wrapper-Protocol composition pattern)
8. D-20-X-multi-model-image-edit-not-implemented (Protocol compliance shim; tracks Spec 21+ image-editing)
9. D-11-9 demo-primary re-evaluation (gated on R-20-4 free-tier verified production-suitable OR AI Enterprise license procured)
10. Spec 21 per-call provider-routing reservation (persona YAML override + router-driven dynamic per-turn selection)
11. Anthropic `list[ReasoningBlock]` arm population (post-T12 follow-up; AnthropicBackend adapter; cross-spec Spec 10/11 conversation-history signature round-trip verification)

#### Per-package version pins at Spec 20 close-out

`persona-core` 1.0.0 unchanged (additive amendments) · `persona-runtime` 0.18.0 unchanged · `persona-api` 0.16.0 unchanged · `persona-web` 0.15.0 unchanged · `persona-voice` 0.V2.0 unchanged. **Spec 20 ships as additive amendments to closed specs (Spec 02 / 05 / 13 / 15 / 18) — no package version bumps required; CHANGELOG entry per surface lands here under `[Unreleased]` until next package-level release.**

---

## [0.V2.0] — 2026-06-10

> **Spec V2 — Streaming Speech-to-Text close-out.** First post-v0.1 release on the voice trunk. persona-voice gains a streaming-STT spine atop V1's settled four-seam substrate. **Per-package version pins at 0.V2.0 cut:** `persona-voice 0.V2.0` candidate (streaming-STT trunk); `persona-core 1.0.0` unchanged; `persona-runtime 0.18.0` unchanged; `persona-api 0.16.0` unchanged; `persona-web 0.15.0` unchanged.
>
> **Headline gates:** 2876 default pytest passed / 2 skipped / 0 regressions (+135 over V1 baseline 2741); 6 V2 integration tests passed at `pytest -m integration`; 4 V2 external smoke tests collected + correctly skipped at `pytest -m external` (operator-pass awaits `PERSONA_STT_API_KEY`); `mypy --strict packages/core/src packages/runtime/src packages/voice/src` clean (156 files); `mypy packages/api/src` clean (56 files); `ruff check + ruff format --check` clean across 49 voice files.
>
> **Additive-precedent chain entry #24** — D-V2-X-streaming-loop-additivity-shape (T07 21 LOC delta at `packages/voice/src/persona_voice/loop/streaming.py:198-217`) is the first post-Spec-19 chain growth (Spec 19 closed at chain 23 per R-19-1 canonical numbering authority).

### Added (Spec V2 — Streaming Speech-to-Text, Phase 6 complete 2026-06-10)

> **`persona-voice 0.V2.0` (streaming STT trunk; first post-v0.1 release).** Provider-independent `StreamingSTT` Protocol mirroring Spec 02 `ChatBackend` verbatim + concrete Deepgram Nova-3 backend (D-V2-1 launch; Speechmatics Ursa 2 documented behind the same Protocol seam as the alternative) + Silero VAD ONNX-only adapter via `silero-vad-lite` (D-V2-X-silero-implementation-shape 3 pillars: ONNX-only path / `SileroFramer` mandatory / lazy-construct + explicit prewarm) + V1 `STTStream` seam adapter (production composition root) + the ONE V1 source delta (T07 StreamingLoop additivity at **21 LOC**; ≤50 architectural-bet budget cleared) + VoiceLog 4 additive STT fields (D-V2-X-cost-discipline) + content-hash-only audit (Spec 15 D-15-X-hard-line-filter mirror) + PERSONA_STT_* env block + integration spine (criterion #2 BINARY proven) + external smoke with 4 measurement gates (operator-pass disposition). **2876 default tests passing + 6 V2 integration + 4 V2 external (skipped without API key); 0 regressions; +135 from V1 close baseline.** Wall-clock onset framing honest per D-V2-2 LOCK: 85-90 ms TYPICAL / 116-121 ms WORST-CASE including `SileroFramer` reframer — V4 semantic turn-detector overlay is the load-bearing FP + timing-tail defense.

- **`feat`: Boundary records + STT domain exceptions** at [`packages/voice/src/persona_voice/stt/{types,errors}.py`](packages/voice/src/persona_voice/stt/) — `SpeechActivityEvent` + `SpeechStartedEvent` + `SpeechEndedEvent` (Pydantic v2 frozen + `extra="forbid"` per D-05-9; discriminated-union `event_type` field; verbatim shape per R-V2-2 v4_consumer_contract). `STTError(ProviderError)` hierarchy mirrors Spec 02 errors.py:30-75: `STTAuthenticationError` / `STTRateLimitError` / `STTStreamFailureError` / `STTAudioFormatError`.
- **`feat`: `StreamingSTT` Protocol + `SpeechActivityListener` Protocol + `StreamingSTTConfig` + `load_streaming_stt` dispatcher** at [`packages/voice/src/persona_voice/stt/{protocol,config,_factory}.py`](packages/voice/src/persona_voice/stt/) — Spec 02 ChatBackend mirror verbatim. `StreamingSTT.transcripts()` returns `AsyncIterator` per D-02-5 (NOT `async def`). `SpeechActivityListener` kept OFF `StreamingSTT` per D-V2-X-activity-listener-shape + Pipecat issue #1323 production-bug precedent (4× duplicate emissions caused by frame re-ordering across a shared seam). `StreamingSTTConfig(BaseSettings, env_prefix="PERSONA_STT_")` with `SecretStr api_key` + Deepgram endpointing/utterance-end + Silero tuning Field constraints.
- **`feat`: Concrete Deepgram Nova-3 streaming-STT backend** at [`packages/voice/src/persona_voice/stt/deepgram_backend.py`](packages/voice/src/persona_voice/stt/deepgram_backend.py) — D-V2-1 LOCK launch provider. `DeepgramStreamingSTT` fail-fasts at construction with `STTAuthenticationError` on missing `PERSONA_STT_API_KEY` (Spec 02 D-02-10); lazy WebSocket open on first `push_audio`; full 401/403/429/400-format/disconnect/domain-passthrough error matrix. Vendor SDK isolated to this module per Spec 02 adapter-boundary discipline. `deepgram-sdk>=4.0,<5` added (MIT + PEP 561 typed + permissive transitive stack). Root `pyproject.toml` adds `[[tool.mypy.overrides]] module = ["deepgram", "deepgram.*"]` mirroring V1 `livekit.*` pattern.
- **`feat`: Silero VAD ONNX-only adapter** at [`packages/voice/src/persona_voice/stt/vad_silero.py`](packages/voice/src/persona_voice/stt/vad_silero.py) — D-V2-X-silero-implementation-shape LOCK. **`SileroFramer`** buffers V1's variable PCM16 cadence into Silero's strict 512-sample / 32 ms windows. **`SileroVADAdapter`** validates Pydantic config at `__init__` + materialises ONNX session only on explicit `load()` call (Spec 02 D-02-10 HFLocalBackend precedent; LiveKit issue #4761 cold-start spike is Windows-scoped upstream — T05 benchmark harness records baseline on actual deployment OS). State machine fires `speech_started` after `min_speech_duration_ms` voiced accumulation; `speech_ended` after `min_silence_duration_ms` silent accumulation. **`session_state_provider` ctor arg** suppresses `speech_started` notification when persona is speaking (D-V2-X-echo-cancellation-v1-dependency mitigation — V1 ships `PassThroughEchoMode.ECHO` pass-through, NOT acoustic echo cancellation; Silero's published ~51 % FP rate on TTS bleed-through is real production risk). **`benchmark_onset_latency`** records P50/P95/P99 wall-clock onset INCLUDING `SileroFramer` overhead — T05 records baseline only; T12 measurement gate #3 (≤150 ms P95) is operator-passed at external smoke. `silero-vad-lite>=0.2,<1` added (MIT; bundles `silero_vad.onnx` v5 + C++ wrapper; NO torch transitive — avoids 200-500 MB).
- **`feat`: V1 STTStream seam adapter (production composition root)** at [`packages/voice/src/persona_voice/stt/seam_adapter.py`](packages/voice/src/persona_voice/stt/seam_adapter.py) — `V1STTStreamSeamAdapter` composes V2's `StreamingSTT` backend + `SileroVADAdapter` into a V1 `STTStream`-Protocol-shaped object (`isinstance(adapter, STTStream)` verified). Tees inbound PCM16 bytes to both backend + VAD via `asyncio.gather`. Two background drainer tasks merge speech-activity events: **Silero VAD events are AUTHORITATIVE** for `speech_started` + **PRIMARY** for `speech_ended`; **provider endpointing events are CORROBORATORS** — re-stamped with `corroborates=True` via Pydantic v2 `model_copy(update=...)` so V4 can weight provider-confirmed endpoints higher without depending on the provider signal for sensor function. Listener dispatch uses `isinstance(event, SpeechStartedEvent)` to narrow the discriminated union. V1 source NOT edited at T06.
- **`feat`: StreamingLoop additivity for `SpeechActivityListener` port (THE ONE V1 source delta of Spec V2)** at [`packages/voice/src/persona_voice/loop/streaming.py:198-217`](packages/voice/src/persona_voice/loop/streaming.py#L198-L217) — D-V2-X-streaming-loop-additivity-shape LOCK. **21 added lines** — additive `speech_activity: SpeechActivityListener | None = None` ctor param + private storage + `@property speech_activity` getter/setter for production composition wiring. **Architectural bet VALIDATED**: ≤50 LOC budget cleared with 60 % margin; the 80 LOC PARTIAL surfacing threshold cleared by far. V1's 12 existing `streaming_loop` tests pass byte-for-byte (no regressions).
- **`feat`: VoiceLog 4 additive STT fields** at [`packages/voice/src/persona_voice/logging.py`](packages/voice/src/persona_voice/logging.py) — D-V2-X-cost-discipline LOCK + D-V1-X-first-token-measurement-coordination. `stt_partial_first_at: datetime | None`, `stt_audio_pushed_at: datetime | None`, `stt_provider_cost_cents_per_minute: float | None` (Deepgram streaming PAYG **$0.0048/min = 0.48 cents/min** per Phase-3 critic correction; $0.0042/min on Growth; the $0.0077/min figure cited in earlier drafts was for pre-recorded transcription, NOT streaming), `stt_total_cents: float | None`. VoiceLog stays frozen Pydantic v2 + `extra="forbid"`; V1's existing 18 tests pass byte-for-byte.
- **`feat`: Content-hash-only transcript audit** at [`packages/voice/src/persona_voice/stt/audit.py`](packages/voice/src/persona_voice/stt/audit.py) — D-V2-X-transcript-content-policy LOCK; Spec 15 D-15-X-hard-line-filter privacy-discipline mirror. `STT_AUDIT_HASH_ALG="sha256"` (aligns with Spec 15's `prompt_sha256`). **At v0.1 raw transcript text NEVER persists** in audit / VoiceLog / credits-ledger. v0.2 candidate gated on operator privacy review + production debugging need + explicit per-conversation opt-in.
- **`feat`: PERSONA_STT_* env-var block** at [`.env.example`](.env.example) — 11 vars; ~66 lines; mirrors PERSONA_IMAGEGEN_* commented-out-with-comments discipline. Operator hint carries Phase-3-critic-corrected pricing + Feb 2026 PAYG concurrency cap tripling to 150 streams + D-V1-5 per-user advisory-lock context + per-language quality-routing fallback to Speechmatics behind the Protocol seam.
- **`feat`: T11 integration spine + T12 external smoke 4 measurement gates** at [`packages/voice/tests/integration/test_v2_streaming_stt.py`](packages/voice/tests/integration/test_v2_streaming_stt.py) + [`packages/voice/tests/external/test_real_provider_smoke.py`](packages/voice/tests/external/test_real_provider_smoke.py) — integration spine asserts criterion #2 BINARY (partials before utterance-end via scripted backend) + #3 + #5 + #6 + #9 (structural negative assertion) + D-V2-X-echo-cancellation-v1-dependency mitigation against real Silero. External smoke ships 4 measurement gates with falsification routes (Speechmatics swap / per-session AR route / V4 overlay tightening / `activation_threshold` → 0.8).

### Cross-spec coordination (Spec V2)

- **Spec 02 ChatBackend pattern mirror** — `StreamingSTT` Protocol + `StreamingSTTConfig` + `load_streaming_stt` dispatcher mirror `ChatBackend` shape verbatim. Vendor SDK (`deepgram-sdk`) isolated to `deepgram_backend.py` per Spec 02 adapter-boundary discipline.
- **Spec 15 D-15-X-hard-line-filter content-hash-only-audit privacy discipline mirror** — V2 inherits at T09: `STT_AUDIT_HASH_ALG="sha256"` aligns with Spec 15's `prompt_sha256`; raw transcripts NEVER persist at v0.1.
- **Spec 18 D-18-X-first-token-measurement-impl coordination** — VoiceLog's `stt_partial_first_at` + `stt_audio_pushed_at` extend the same number-shape Spec 18 records at `runtime/loop.py:465-468` (D-V1-X-first-token-measurement-coordination — one measurement convention, N producers).
- **V1 architectural validation** — D-V2-X-streaming-loop-additivity-shape LOCK (T07) at 21 LOC validates the V1 `StreamingLoop` ctor's additive-port shape is the structurally correct one for V2/V4/V5/V6 seam integration.
- **D-V2-X-echo-cancellation-v1-dependency** — V1 closeout.md does not document AEC; V1 ships pass-through ECHO mode. V0.1 mitigation is T05's `session_state_provider` mute-window safety net; v0.2 production-grade fix is V1 transport-layer AEC. MAINTENANCE.md carries the operator-pass deploy commitment row.

---

## [v0.1.0] — 2026-06-07

> **Project v0.1.0 release.** The first demoable system release per architecture §10 + D-11-8 (library 1.0.0 = stable public API under Apache 2.0; product v0.1.0 = first demoable system). Aggregates the Phase 2 close-outs (Spec V1 voice trunk, Spec F4 rich-output UI, Spec 18 unified router, Spec F3 file-input UI) and the Spec 19 amendment set (10 additive chain entries 13–22 + the memory_chunks.kind CHECK migration L9) landed during the v0.1 close-out window.
>
> **Per-package version pins at v0.1.0 cut:**
> - `persona-core 1.0.0` (first public Apache-2.0 stable per D-11-8) — see [`packages/core/CHANGELOG.md`](packages/core/CHANGELOG.md).
> - `persona-runtime 0.18.0` — see [`packages/runtime/CHANGELOG.md`](packages/runtime/CHANGELOG.md).
> - `persona-api 0.16.0` — see [`packages/api/CHANGELOG.md`](packages/api/CHANGELOG.md).
> - `persona-web 0.15.0` — see [`packages/web/CHANGELOG.md`](packages/web/CHANGELOG.md).
> - `persona-voice 0.1.0` (first release per V1 close) — see [`packages/voice/CHANGELOG.md`](packages/voice/CHANGELOG.md).
>
> **Spec 19 amendment chain (10 entries):** L1 (chain 13) D-19-X-prompt-builder-produced-files-verification (persona-runtime); L2 (chain 14) D-19-X-file-write-produced-files (persona-core); L4 (chain 15) D-19-X-host-out-debug-logging (persona-core); L3 (chain 16) D-19-X-imagegen-env-documentation (docs); L5 (chain 17) D-19-X-hosting-topology-amendment (docs); L6a (chain 18) D-19-X-low-balance-warning-ui (persona-web); L6b (chain 19) D-19-X-voice-token-credit-gate (persona-voice); L6c (chain 20) D-19-X-credits-service-domain-relocation (persona-core + persona-api); L7 (chain 21) D-19-X-spec14-integration-test (persona-api/tests); L8 (chain 22) D-19-X-mypy-path-pin (scripts/ + README); L9 (chain 23) D-19-X-memory-chunks-kind-check-migration (persona-api).

### Added (Spec V1 — Real-Time Voice Service and WebRTC Transport, Phase 6 complete)

> **`persona-voice 0.1.0` (new workspace member).** The voice trunk — a 4th uv workspace package + LiveKit OSS substrate + WebRTC transport facade + session lifecycle + streaming-loop skeleton with V2/V3/V4/V5 Protocol seams + advisory-lock per-user concurrency + VoiceLog instrumentation. Branch (A) per D-V1-1 (R-V1-1 ruled out aiortc on documented 17–20× latency overhead). **2712 default tests passing, 0 regressions** (+178 from Spec V1 work alone — 84 voice unit + 5 voice integration against live LiveKit Server + Postgres + 10 persona-core auth tests from the T03 extraction). Binary criterion #3 (full-duplex) **structurally proven** via live LiveKit Server.

- **`feat`: `packages/voice/` as 4th uv workspace member** with `persona-core[postgres]` + `livekit>=1.1,<2` + `livekit-api>=1.1,<2` deps (both Apache-2.0 — D-V1-X-livekit-sdk-license-stack confirmed via PyPI). Root `pyproject.toml` extended with mypy_path + pytest testpaths + `livekit.*` mypy-override + per-file ruff ignore. Root `conftest.py` adds `packages/voice/src` for the editable-`.pth` iCloud-hidden-flag workaround.
- **`feat`: JWT verifier extraction to persona-core** at [`packages/core/src/persona/auth/jwt_verifier.py`](packages/core/src/persona/auth/jwt_verifier.py) (D-V1-X-jwt-verifier-extraction; additive Spec 08 amendment per D-12-X / D-16-X / D-F4-X-bare-ref-resolution precedent chain). `make_jwt_verifier(config)` + `AuthenticatedUser` + new `JwtVerifierConfig: Protocol` (structural subtype; APIConfig + VoiceConfig satisfy implicitly via `@property` for `jwt_algorithms_list`). `AuthenticationError` relocated to `persona.errors`; `python-jose[cryptography]` moved to persona-core deps. persona-api `auth/deps.py` + `errors.py` re-export for back-compat — `test_api_auth.py` passes byte-for-byte.
- **`feat`: Token-issuance endpoint** at [`packages/voice/src/persona_voice/http/app.py`](packages/voice/src/persona_voice/http/app.py) — `POST /v1/voice/token` (JWT-authed via the extracted verifier); checks persona ownership via the configured DB; mints a LiveKit `AccessToken` (room=`persona:<session_id>`; identity=user_id; metadata={persona_id, conversation_id, session_id}; TTL 10min default); returns `{token, room_name, livekit_url}`. RLS-shape 404 on cross-tenant.
- **`feat`: `VoiceRoom` facade over `livekit.rtc.Room`** at [`packages/voice/src/persona_voice/transport/room.py`](packages/voice/src/persona_voice/transport/room.py) — connect / disconnect / `track_subscribed` → `InboundAudioFrame` drain (resampled to canonical PCM16 mono 16 kHz per D-V1-6) / `publish_outbound` (PCM16 mono 24 kHz) / `capture_outbound_frame` / `RoomSubstrate: Protocol` for test substrate injection. `build_voice_room()` is the production constructor.
- **`feat`: Session lifecycle state machine** at [`packages/voice/src/persona_voice/session/state_machine.py`](packages/voice/src/persona_voice/session/state_machine.py) — `Session` frozen Pydantic v2 + `SessionState = Literal["created","active","ended"]` + `SessionLifecycleEvent` StrEnum (7 V4-aligned values) + `SessionEventListener` Protocol + `InvalidSessionStateError`. `make_session_rls_engine(url, user_id)` is the per-session RLS engine (D-V1-X-rls-engine-shape, pool_size=1, user_id baked into checkout listener). `attach_to_room(voice_room)` wires `Room.on('disconnected')` → `end()` → engine.dispose → advisory-lock release via tx rollback.
- **`feat`: Streaming-loop skeleton with V2/V3/V4/V5 Protocol seams** at [`packages/voice/src/persona_voice/loop/streaming.py`](packages/voice/src/persona_voice/loop/streaming.py) — `STTStream` (V2 push: `push_audio` + `transcripts() -> AsyncIterator[Transcript]`); `TTSStream` (V3: `synthesize(text_stream: AsyncIterator[str]) -> AsyncIterator[AudioChunk]` + `cancel()` for V4 barge-in); `ModelReplyProducer` (V5: `(final_transcript) -> AsyncIterator[str]`); V4 reuses `SessionEventListener` from T06. `PassThroughEchoMode` StrEnum (ECHO/DISABLED) — the pass-through default that lets T08 prove full-duplex before V2/V3/V5 wire intelligence. V2→V5→V3 pipeline runs as `asyncio` Task; D-V1-6 sample-rate guard raises on mismatch.
- **`feat`: Per-user voice-call concurrency** at [`packages/voice/src/persona_voice/concurrency.py`](packages/voice/src/persona_voice/concurrency.py) — `acquire_voice_call_concurrency(*, conn, user_id)` mirrors `imagegen/concurrency.py` verbatim per D-V1-X-d15x-precedent-binding. `pg_try_advisory_xact_lock(('x' || md5(:user_id))::bit(64)::bigint)` auto-releases on tx commit/rollback; multi-worker-correct from day one. `VoiceConcurrencyCappedError(PersonaError)` analogue maps to 429 + Retry-After at the endpoint integration site (post-V1).
- **`feat`: VoiceLog instrumentation** at [`packages/voice/src/persona_voice/logging.py`](packages/voice/src/persona_voice/logging.py) — frozen Pydantic v2 + `extra="forbid"` per D-05-9. LiveKit canonical hops (`eou_at` / `stt_final_at` / `llm_first_token_at` / `tts_first_byte_at` / `audio_first_play_at`) coordinated with Spec 18 D-18-X-first-token-measurement-impl per D-V1-X-first-token-measurement-coordination. V1's binding share (`transport_in_ms` / `transport_out_ms` / `loop_overhead_ms`; 100ms P50 / 150ms P95 CI gate). `JSONLVoiceLogWriter` durable per-write flush.
- **`feat`: T08 binary criterion #3 PROVEN** at [`packages/voice/tests/integration/test_full_duplex.py`](packages/voice/tests/integration/test_full_duplex.py) — agent (persona-voice's `VoiceRoom`) + client (raw `rtc.Room`) join the same LiveKit Server Room; publish/subscribe a 2s sine tone in BOTH directions concurrently; both ends receive ≥10 frames at the canonical D-V1-6 rates. Full-duplex is structurally proven; V4 barge-in foundation is real, not aspirational.

### Cross-spec coordination (Spec V1)

- **Spec 08 additive amendment (9th in chain)** — `make_jwt_verifier` + `AuthenticatedUser` extracted to `persona.auth.jwt_verifier`. persona-api re-exports; no test breakage. Per D-12-X / D-16-X precedent.
- **Spec 15 D-15-X-concurrency-cap precedent binding** — `acquire_voice_call_concurrency` is the verbatim mirror of `imagegen/concurrency.py`. The kickoff's "Postgres rate-limit table" generic lean was wrong; corrected at Phase 1 and locked at Phase 4.
- **Spec 18 D-18-X-first-token-measurement-impl coordination** — VoiceLog's `llm_first_token_at` field uses the same shape Spec 18 records at `runtime/loop.py:465-468`. One measurement convention, two producers, V5 reads from both.
- **Spec 11 D-11-1/2/3 hosting amendment** — `livekit-server` Go binary becomes a sidecar container in docker-compose; v0.1 single-VPS sizing reviewed at MAINTENANCE.md.
- **Architecture §10 voice OOS supersession** — line 769 "Voice. Out of scope for September." superseded by a new §11-equivalent voice-layer block. Additive precedent chain: 10th entry per D-V1-X-architecture-md-update.

### Decisions (Spec V1)

23 decisions locked at Phase 4 per [`docs/specs/phase2/spec_V1/decisions.md`](docs/specs/phase2/spec_V1/decisions.md). Headline: **D-V1-1 branch (A) — self-hosted LiveKit OSS Server + `livekit` low-level Python SDK + hand-implemented Protocols**. Rejected aiortc on R-V1-1's documented evidence (Issue #775 500–600ms LAN Opus latency 17–20× over the 30ms budget; Issue #505 SRTP blocks asyncio event loop; unfixed memory leaks); rejected language change because it defeats in-process persona-core access. Cloudflare Realtime TURN primary + Twilio NTS fallback (D-V1-2; $0 + $5/mo at v0.1 scale). All 23 mirrored to [`docs/DECISIONS.md`](docs/DECISIONS.md).

### Added (Spec F4 — Rich-Output UI Surface, Phase 5 complete; Phase 6 pending operator-pass + sign-off)

> **`persona-web 0.15.0` + `persona-api 0.x.y` + `persona-runtime 0.x.y` candidates.** Capability-UI spec built entirely from F2 primitives consuming Spec 12 + 15 + 16 + 17 + 13 + 06/08. Two additive backend amendments — both in the closed-spec additive-extension precedent (10th entry: D-F4-X-bare-ref-resolution). No new design tokens. **599 vitest tests across 54 files** (F3 baseline 400 → +199); full check matrix green across web + api + runtime.

- **`feat`: `OutputContent` discriminated union + Zod schema** at [`packages/web/src/lib/api/output-content.ts`](packages/web/src/lib/api/output-content.ts) — six variants (`inline-image` / `inline-chart` / `download-doc` / `result-block` / `working` / `failure`) with `kind` discriminator; `.strict()` per variant mirrors Pydantic `extra="forbid"`. D-F4-X-renderer-normaliser-shape.
- **`feat`: chat + run normalisers via shared `_classify.ts`** at [`packages/web/src/lib/normalisers/`](packages/web/src/lib/normalisers/) — `chatSseToOutputContent(event)` and `runEventToOutputContent(event)` produce IDENTICAL OutputContent for the same produced_file payload. Transport-shape leakage stops here (D-09-1).
- **`feat`: `RunStep.outputs` view-time derivation** in [`packages/web/src/lib/run.ts`](packages/web/src/lib/run.ts) — `runViewFromEvents` accumulates per-step `outputs: OutputContent[]` from tool_calling + tool_result events; no backend `Step` schema change (D-F4-X-output-derivation-shape).
- **`feat`: F4 renderer set** at [`packages/web/src/components/chat/output/`](packages/web/src/components/chat/output/) — `<InlineVisual>` (R-F4-4 one-component with intent prop) + `<DownloadChip>` (Bearer-auth blob download) + `<ResultBlock>` (monospace + truncation + collapsible Shiki code via React.lazy + Suspense) + `<WorkingState>` (F1 ToolRunningIndicator visual reused verbatim) + `<OutputDispatcher>` + `<OutputList>` (six-variant exhaustive switch + path-traversal defence-in-depth) + `<ImageLightbox>` (portal modal with ESC/backdrop/close).
- **`feat`: MessageElement + StepCard surface integration** — `message-element.tsx` InterleavedContent emits dispatcher per recognized capability tool alongside ToolCallCard; `step-card.tsx` consumes derived `step.outputs` via `<OutputList>`. SAME renderer set across both surfaces.
- **`feat`: `<AuthedImage>` F2 promotion** — strangler-fig move to `src/components/ui/authed-image.tsx`; re-export shim at the F3 path preserves all existing imports (D-F4-X-authedimage-f2-promotion).
- **`amendment`: `RunEvent.tool_result` constructor at [`packages/runtime/src/persona_runtime/agentic/events.py:96-103`](packages/runtime/src/persona_runtime/agentic/events.py#L96-L103)** — 4-line additive edit (Option A) forwards `result.data.produced_files` onto the event payload. Same constructor serves BOTH chat SSE AND RunEvent transports per the docstring self-naming → ONE edit lights up both normalisers. No Pydantic schema change. D-F4-X-event-kind-for-produced-files.
- **`amendment`: `_persist_produced_file` policy at [`packages/api/src/persona_api/sandbox/runtime_tool.py:216-244`](packages/api/src/persona_api/sandbox/runtime_tool.py#L216-L244)** — D-F4-X-bare-ref-resolution three-branch persister policy fix. **THE Phase 3 R-F4-1 catch: Spec 16 doc downloads were 404ing in production; T02c fixes at the producer side.** Charts/ + intermediate/ stay at workspace root (load-bearing); everything else routes into `uploads/<filename>.<ext>` so the slash-aware resolver lands on the right path. 9 regression tests.
- **`feat`: structural invariants test surface** at [`packages/web/src/components/chat/output/__tests__/structural-invariants.test.tsx`](packages/web/src/components/chat/output/__tests__/structural-invariants.test.tsx) — six cross-cutting structural assertions: dispatcher exhaustiveness, 1MB-stays-by-reference (F3 T22 mirror), single-renderer-set parity across transports, path-traversal swap-to-failure, cross-surface DOM identity, dispatch-table parity with R-F4-1. 35 tests.
- **`feat`: Playwright scaffold** at [`packages/web/e2e/f4-rich-output.spec.ts`](packages/web/e2e/f4-rich-output.spec.ts) — 8 journeys (7 acceptance criteria coverage + 1 structural invariant journey); CSA-3 🟦 operator-passed at sign-off.

### Decisions (Spec F4)

> 18 decisions lock at Phase 4 + 1 future-tracking MAINTENANCE.md event-driven row (D-F4-Y-producer-kind). Full rationale in [`docs/specs/phase2/spec_F4/decisions.md`](docs/specs/phase2/spec_F4/decisions.md) + the project-wide one-liner mirror at [`docs/DECISIONS.md`](docs/DECISIONS.md).

### Cross-spec coordination (Spec F4 → Spec 12/16/17 + F2)

- **F4 → Spec 12 (×2):** additive `events.py:96-103` constructor edit + additive `runtime_tool.py:_persist_produced_file` policy edit. Closed-spec extensions; no Spec 12 re-open.
- **F4 → Spec 16:** D-F4-X-bare-ref-resolution editorial note staged for `spec_16/closeout.md` ("Spec 16 doc downloads were 404ing in production until F4 Phase 5; fixed via `runtime_tool.py:231` (2026-06-07)").
- **F4 → Spec 17:** `charts/<id>.png` policy unchanged (load-bearing for D-17-X-inline-hint-shape).
- **F4 → F2:** `<AuthedImage>` promoted to canonical F2 home via strangler-fig.
- **Additive-extension precedent chain hits 10 entries** with D-F4-X-bare-ref-resolution — pattern fully crystallised.

### Known limitations (Spec F4 — production-honest)

- **Persisted-step rich-output rendering degraded:** the `Step.model_dump` persisted snapshot doesn't carry structured `produced_files` — only the live `RunEvent` event-log path benefits from F4's `step.outputs[]`. Completed runs viewed later degrade to existing tool-card render. **Fix path:** additive amendment to backend `Step` Pydantic + persistence; tracked as future Spec 06/F4 follow-up.
- **8 Playwright journeys + dark/light + mobile reference-composition spot-checks** deferred to 🟦 operator-pass at sign-off — full stack provisioning needed.
- **F3 re-export shim** at `src/components/chat/authed-image.tsx` removable once all callers migrate to `@/components/ui/authed-image` — low priority.

### Added (Spec 18 — Unified Model Router, Phase 5 + 6 close-out)

> **`persona-runtime 0.18.0` candidate.** Upgrades the Spec 05 rule-based router into a pluggable `Router` Protocol with a layered architecture (Layer 1 capability hard-filter + Layer 2 sweet-spot scorer over cost / quality / latency, weighted per profile). Subsumes V5's voice-latency routing as the `"voice"` profile. Strangler-fig discipline preserves the Spec 05 byte-for-byte: existing `test_router.py` 25/25 + `test_router_vision.py` 10/10 pass unchanged. 138 new runtime unit tests; 411 total runtime unit tests green; mypy --strict clean across 123 source files.

- **`feat`: `Router` + `RouterScorer` Protocols** at [`packages/runtime/src/persona_runtime/routing/protocol.py`](packages/runtime/src/persona_runtime/routing/protocol.py) — `@runtime_checkable`. `Router.route(context: RoutingContext) -> RoutingDecision`. `RouterScorer` is the v0.2 extras seam for the optional learned-router integration (D-18-1); v0.1 ships zero `RouterScorer` implementations — internal heuristic scorer is the production default per R-18-1.
- **`feat`: `HeuristicRouter`** at [`packages/runtime/src/persona_runtime/routing/heuristic.py`](packages/runtime/src/persona_runtime/routing/heuristic.py) — Spec 05's rule-based router refactored behind the Protocol. `.choose()` preserved verbatim (byte-for-byte regression guarded). `.route()` is the new Protocol entry. **Strangler-fig alias** at [`router.py`](packages/runtime/src/persona_runtime/router.py) re-exports `HeuristicRouter as Router` — 18 of 19 reference sites (production + tests) zero-touch per D-18-X-strangler-fig-alias-shape.
- **`feat`: `UnifiedRouter`** at [`packages/runtime/src/persona_runtime/routing/unified.py`](packages/runtime/src/persona_runtime/routing/unified.py) — Layer 1 (hard filter via `apply_constraint_filter`) + Layer 2 (sweet-spot scorer) + bounded fallback (voice 30ms / text 100ms per D-18-4). Falls back to embedded `HeuristicRouter` on scoring error / empty metadata / bound exceedance. 4 fallback reasons: `"timeout"` / `"scoring_error"` / `"empty_metadata"` / `"partial_metadata:<tier>"` with rate-limited `loguru.warning` per (reason, profile) per 60s (D-18-X-fallback-instrumentation).
- **`feat`: `apply_constraint_filter` free function** at [`packages/runtime/src/persona_runtime/routing/layer1.py`](packages/runtime/src/persona_runtime/routing/layer1.py) — shared by `HeuristicRouter.route()` AND `UnifiedRouter.route()` via module-level import (D-18-X-layer1-extraction). Three constraints: vision, context-window (graceful when metadata absent), tool-strength (graceful when absent). **T-layer1-invariant** test at [`test_routing_layer1_invariant.py`](packages/runtime/tests/unit/test_routing_layer1_invariant.py) patches the function at module level; verifies both routers honour it.
- **`feat`: `RoutingContext` + `RoutingDecision` boundary types** at [`packages/runtime/src/persona_runtime/routing/types.py`](packages/runtime/src/persona_runtime/routing/types.py) — frozen Pydantic v2 + `extra="forbid"` (D-05-9 precedent). `RoutingContext` carries the 8 turn signals (vision / tokens / tools / first_turn / identity_sensitive / boilerplate / phase / profile); `RoutingDecision` carries tier + model + rationale + candidates + Layer 1 reasons + Layer 2 score + fallback_triggered + fallback_reason.
- **`feat`: `TierMetadata` + `TierRegistry.metadata_for()`** at [`packages/runtime/src/persona_runtime/tier.py`](packages/runtime/src/persona_runtime/tier.py) — additive extension at the **runtime layer** (NOT on `ChatBackend` Protocol per the Phase 1 fold-in d). 6 fields per D-18-3: cost_input/output_per_1k, first_token_latency_ms, throughput_tokens_per_sec, context_window, tool_strength. `tier_metadata_from_env(prefix)` ships the env-var population path. `TierConfig.metadata` defaults to `None` so existing constructions stay valid.
- **`feat`: `FirstTokenLatencyTracker`** at [`packages/runtime/src/persona_runtime/routing/latency.py`](packages/runtime/src/persona_runtime/routing/latency.py) — per-model EWMA tracker (α=0.2) with simple-average warm-up for samples 1-5 (D-18-X-first-token-measurement-impl). Hooked into `ConversationLoop._stream_round` at the first non-empty `chunk.delta`. V5 R-V5-1 coordination locked: one measurement, two consumers.
- **`feat`: TurnLog routing extension** at [`packages/runtime/src/persona_runtime/logging.py`](packages/runtime/src/persona_runtime/logging.py) — additive D-18-X-turnlog-extension fields: `routing_decision: RoutingDecision | None`, `routing_latency_ms: float`, `routing_fallback_triggered: bool`, `routing_fallback_reason: str | None`. Pre-Spec-18 callers stay green (all optional with safe defaults). JSON round-trip verified for Postgres JSONB compatibility.
- **`feat`: `RoutingConstraintsUnsatisfiableError`** at [`packages/core/src/persona/backends/errors.py`](packages/core/src/persona/backends/errors.py) — new generalised Layer 1 fail-loud parent class per D-18-X-constraint-failure-shape with structured `{"reason", "configured_tiers", "required"}` context. `NoVisionTierConfiguredError` becomes a subclass; existing Spec 13 raise site + assertions stay valid.
- **`feat`: `tools/routing_eval/` harness** — N=10 labelled YAML fixture at [`tools/routing_eval/fixtures/representative_turns.yaml`](tools/routing_eval/fixtures/representative_turns.yaml) (R-18-4 starter set, grows via PR like any test fixture); `replay.py` CI-runnable regression test that asserts every fixture entry's `expected_tier` matches the router's choice; `aggregate.py` manual tool that reads TurnLog JSONL and prints per-tier distribution + fallback rate (thresholded per D-18-X-fallback-instrumentation healthy/watch/alert/force-heuristic) + latency percentiles + per-tier cost histogram.

### Decisions (Spec 18)

> 19 decisions land at Phase 4: 6 standard (D-18-1..D-18-6) + 5 cross-spec micros (registry-granularity / constraint-failure-shape / turnlog-extension / protocol-location / latency-measurement-source) + 2 Phase 1 surfaced (strangler-fig-alias-shape / agentic-loop-routing-coupling) + 3 Phase 3 surfaced (layer1-extraction / partial-metadata-behaviour / monthly-review-cadence) + 3 Phase 4 fold-in surfaced (first-token-measurement-impl / fallback-instrumentation / routing-eval-shape). All resolved in Phase 4; held under Phase 5 implementation. Full rationale in [`docs/specs/phase2/spec_18/decisions.md`](docs/specs/phase2/spec_18/decisions.md) + the project-wide one-liner mirror at [`docs/DECISIONS.md`](docs/DECISIONS.md).

### Known limitations (Spec 18 — production-honest)

- **D-18-5 quality_proxy formula softens Spec 05's "first turn → frontier" rule.** With the locked 0.30 weight, first-turn signal alone produces quality_proxy=0.30, which routes to mid (whose quality_estimate=0.5 is closer than frontier's 1.0) under text profile. The Layer 2 cost-balanced weighting wins. **v0.2 candidate** if production telemetry shows quality drift on first turns: raise `is_first_turn` weight. Documented in fixture entry #2 notes.
- **`first_token_latency_ms` measurement persists in-process only at v0.1** — warm-up converges in ~5 turns so cross-restart persistence is YAGNI. v0.2 may persist via the TurnLog JSONL path if telemetry surfaces post-restart cold-start routing degradation.
- **`RouterScorer` Protocol seam ships with zero implementations** — v0.2 candidate for RouteLLM-mf (or similar) behind a `persona-runtime[learned-router]` extras gate per the R-18-1 survey conclusion. v0.2 candidacy gated on production telemetry surfacing the internal heuristic scorer is genuinely wrong on a meaningful fraction of turns.

### Added (Spec F3 — File-Input UI Surface, Phase 5 complete; Phase 6 pending operator-pass + sign-off)

> **`persona-web 0.14.0` candidate.** Capability-UI spec built entirely from F2 primitives consuming Spec 13 (vision) + Spec 14 (document ingestion) + Spec 08 (chat message endpoint) contracts. No new backend surface (T02's `PersonaCapabilities` is the single additive field on `PersonaDetail`). No new design tokens. All 19 D-F3-* decisions validated with named test surfaces; 400 Vitest tests across 44 files (F2 baseline 233 → +167); full check matrix green across web + api.

- **`feat`: composer file-attach surface** — single attach control with content-type dispatch (D-F3-1); image preview tray (D-F3-X-preview-placement); conversation-scoped document panel (D-F3-2 chip-only); inline image render in sent message bubbles via Bearer-authed blob URLs (D-F3-X-image-serve-auth); at-send `NoVisionErrorBanner` safety net (D-F3-X-no-vision-surface-shape (c)); deployment-honest no-vision tooltip on the disabled attach button (D-F3-X-no-vision-tooltip-copy + D-F3-X-deployment-vs-persona-capability-framing).
- **`feat`: `PersonaDetail.capabilities` additive field** at [`packages/api/src/persona_api/schemas/responses.py`](packages/api/src/persona_api/schemas/responses.py) — `{vision: bool, configured_tiers: tuple[str, ...]}`. Hydrated via the public `TierRegistry.supports_vision_for(name)` + `configured_tier_names` contract (D-F3-X-tier-registry-public-contract), NEVER the private `_VISION_CAPABILITY` matrix. Deployment-derived at v0.1; field shape survives the v0.2 per-persona-tier-pin migration unchanged (D-F3-X-deployment-vs-persona-capability-framing).
- **`feat`: composer state slice + upload orchestration** at [`packages/web/src/components/chat/composer/use-composer-attachments.ts`](packages/web/src/components/chat/composer/use-composer-attachments.ts) — image-attached state + per-image upload progress + per-image retry/remove (D-F3-X-partial-upload-failure-shape). Conversation-switch state reset via sole-dep `useEffect(() => setAttachedImages([]), [conversationId])` (D-F3-X-cap-attached-state-on-conversation-switch). Document panel state lives separately in `useConversationDocuments` (conversation-scoped).
- **`feat`: shared multipart upload service** at [`packages/web/src/lib/upload.ts`](packages/web/src/lib/upload.ts) — `uploadImage(personaId, file, ...)` + `uploadDocument(personaId, conversationId, file, ...)` against the CSA-2 content-type-dispatching endpoint. XMLHttpRequest for byte-level upload progress (D-F3-4: real progress >1MB, indeterminate <1MB); AbortController with two-phase early-exit; Bearer token + structured ApiError mapping.
- **`feat`: `useAuthedImageBlobUrl` hook** at [`packages/web/src/lib/hooks/use-authed-image-blob-url.ts`](packages/web/src/lib/hooks/use-authed-image-blob-url.ts) — fetch with Bearer auth, blob → `URL.createObjectURL`. Full 4-behaviour discipline asserted in tests: (a) AbortController cancels in-flight on unmount/ref-change; (b) `URL.revokeObjectURL` fires on both transitions; (c) 404 → null src + null error (existence-disclosure-safe per D-08-1); (d) 5xx → error set for retry. v0.2 path: signed-URL endpoint (see "Production hardening, next minor" below).
- **`feat`: `useObjectURL` hook** at [`packages/web/src/lib/hooks/use-object-url.ts`](packages/web/src/lib/hooks/use-object-url.ts) — composer-local preview URL lifecycle with full 3-transition cleanup discipline (unmount + file-change + null) per D-F3-X-preview-cleanup-discipline. Distinct from `useAuthedImageBlobUrl` (server-fetch path); two clean lifecycles.
- **`feat`: `useChat.send` strangler-fig extension** at [`packages/web/src/lib/hooks/use-chat.ts`](packages/web/src/lib/hooks/use-chat.ts) — accepts optional `attachedImages: ImageRef[]`; threads them into `PostMessageRequest.images`. SSE consumption + `RunEvent` envelope (D-F2-15 / D-09-1) + error-toast routing + reconnect behaviour ALL UNCHANGED. Optimistic user-turn carries `images` so the bubble renders the just-attached image inline before history reload.
- **`feat`: F3-local composer components** at [`packages/web/src/components/chat/composer/`](packages/web/src/components/chat/composer/) — `<ComposerAttachControl>` (button + hidden input + content-type dispatch), `<ComposerImagePreview>` (thumbnail + remove + state cue), `<DocumentChip>` (icon + truncating name + size + remove + scanned-PDF cue), `<ConversationDocumentList>` (panel), `<NoVisionErrorBanner>` (at-send safety net). F3-local per D-F3-X-chip-placement / D-F3-X-preview-placement; promote to F2 on F4/F5 second-consumer reuse.
- **`feat`: drag-and-drop + paste handlers** at [`packages/web/src/components/chat/composer/use-attach-non-click.ts`](packages/web/src/components/chat/composer/use-attach-non-click.ts) — desktop-only enhancements per X-F3-3. Folder-drop rejection (`webkitGetAsEntry().isFile === false`); remote-URL drag silently skipped (`kind === "string"`); image-paste only on textarea (document attach stays button-only per Mac convention).
- **`feat`: `packages/web/src/lib/api/limits.ts`** — API-sourced caps with `// API source: <file>:<line>` comments on every constant. `MAX_DOCUMENTS_PER_CONVERSATION: number | null = null` shape (D-F3-X-document-conversation-count-cap) — v0.2 swaps the value without TypeScript surgery.
- **`security`: store-by-reference structural defence** at [`packages/web/src/lib/hooks/use-chat-body-size.test.ts`](packages/web/src/lib/hooks/use-chat-body-size.test.ts) — three STRUCTURAL regression tests enforce Concern #4 production-safety invariant: (a) 4×1MB-ref message body < 2 KB; (b) text-only body < 500 B (no shape drift from F3 extension); (c) body size linear in reference count, not image bytes. **API-call-layer mirror of Spec 13 T13's DB-layer guard.** If anyone ever inlines base64 image bytes into the chat-message body, all three tests fail loud.
- **`security`: T20 ARIA-via-`t()` structural discipline** at [`packages/web/src/components/chat/composer/composer-a11y.test.ts`](packages/web/src/components/chat/composer/composer-a11y.test.ts) — source-grep across all 5 composer components asserts every `aria-label=` uses `t()` or a variable, NEVER a raw English literal. `pnpm check:no-literals` catches CSS literals but not JSX attribute literals; this fills the gap. Plus 19 i18n-key resolution assertions confirming every composer key resolves in `en.json`.
- **`feat`: T01 `gen-api.sh` surgical fix** at [`packages/web/scripts/gen-api.sh`](packages/web/scripts/gen-api.sh) — `PYTHONPATH` workaround for the Spec 01 D-01-9 surprise (uv 0.6.x writes editable installs as `_editable_impl_*.pth`; CPython 3.13 treats underscore-prefixed .pth files as hidden). Mirrors the existing `conftest.py` workaround pattern. Drop when uv ships a release without the underscore prefix. **Phase 6 coordination note for Spec 01 / Spec 08 owner**: candidate for project-wide `Makefile` / `pyproject.toml` formalisation.

### Known limitations (Spec F3 — production-honest)

- **No per-conversation document count cap at v0.1.** Spec 14 ships no server-side cap; web's `limits.ts` mirrors with `MAX_DOCUMENTS_PER_CONVERSATION: number | null = null`. **Abuse-prevention surface**: a single user could attach 1,000+ documents to one conversation. Flagged as a **Spec 14 follow-up** (not F3 scope) with concrete production trigger: the first time a single conversation exceeds N documents in production telemetry (N TBD; defensive minimum ~50). D-F3-X-document-conversation-count-cap records the v0.1 disposition.
- **Composer icon-button tap target is 32px (F2 `size-icon` variant)**, below the iOS HIG 44px guideline. The invisible click expansion + adjacent textarea form a generous tap region in practice. Cross-cutting F2 change (size-up to `size-11` = 44px) is the v0.2 fix; not F3 scope.
- **No automated Lighthouse score on `/chat`.** Inherited from Spec 09 #10 — auth-gated chat route needs Clerk-session injection for headless Lighthouse; manual pre-demo check stands. F3 adds no new Lighthouse risk (blob URLs for previews stay client-local; XHR upload is event-driven, not bundle-impact).

### Production hardening — next minor (reframed from v0.2-deferral)

Three items reframed at Phase 5 sign-off from "v0.2 candidate" to "production-merit improvements ready for the next minor":

- **Signed-URL pattern for image serve.** `useAuthedImageBlobUrl` today fetches with Bearer + creates an object URL — works but adds a round-trip and memory churn. Signed URLs would let `<img src>` point directly at a time-limited URL: faster image load, simpler error surface, cleanest cross-token-domain pattern when BYOK lands. **Trigger:** image-serve latency metrics show user-visible degradation OR BYOK lands.
- **`GET /v1/limits` endpoint.** Today `limits.ts` mirrors API constants via `// API source:` comments — works but introduces drift risk on the next API-constant change. A `/v1/limits` endpoint sourced from a Pydantic Settings model = canonical limits, web fetches-once + caches, drift impossible. **Trigger:** first API-constant drift event detected.

### Pure v0.2 scope-boundary items (NOT production-hardening)

- Rich first-page document preview — UX enhancement, not v0.1 capability gap. Chip-only ships honestly.
- Per-persona tier pins → genuinely per-persona capabilities — different feature, tied to per-persona deployment work. `PersonaDetail.capabilities` field shape already survives the migration.

### Decisions (Spec F3)

Full rationale in [`docs/specs/phase2/spec_F3/decisions.md`](docs/specs/phase2/spec_F3/decisions.md); per-decision one-liners in [`docs/DECISIONS.md`](docs/DECISIONS.md). Highlights:

- **D-F3-1** one attach control with content-type dispatch + clear post-attach feedback.
- **D-F3-X-capability-endpoint** additive `capabilities: PersonaCapabilities` field on `PersonaDetail`; no new endpoint.
- **D-F3-X-deployment-vs-persona-capability-framing** vision is deployment-derived at v0.1; identical for every persona under a given deployment. v0.2 inflection point — field shape survives unchanged.
- **D-F3-X-image-serve-auth** `useAuthedImageBlobUrl` hook with full 4-behaviour discipline (AbortController + revoke on unmount/ref-change + 404 placeholder + 5xx retry).
- **D-F3-X-partial-upload-failure-shape** block send while ANY image is uploading OR error; per-image retry + remove (fail-loud over silent-drop).
- **D-F3-X-cap-attached-state-on-conversation-switch** sole-dep `[conversationId]` reset; image attachments are message-scoped.
- **D-F3-X-capabilities-prop-drill-shape** prop drill PersonaDetail → ChatWindow → ComposerAttachControl. NOT context, NOT global.
- **D-F3-X-closeout-operator-pass-convention** 🟦 operator-passed vs ✅ MET in-CI as the explicit two-class disposition. **Now used in Spec 15 / Spec 16 / F3** — promotion to project-wide convention pending in Phase 6 close-out.

## [0.15.1] — 2026-06-06

> **`persona-core` patch.** Bundles four spec close-outs that signed off on 2026-06-06 and were staged in `[Unreleased]`: Spec 17 (data analysis), Spec 12 (code execution sandbox; Phase 5+6 close-out), Spec 14 (document ingestion), Spec 16 (document generation skills). Plus the Spec-12-additive surfaced by Spec 16 Phase 5b (D-12-X-venv-path-ordering) and the Spec 16 "Fixed" / "Inherited" sub-sections. All additive — no breaking-change coupling with persona-web; F3 (persona-web) ships separately when its Phase 6 operator-pass completes. The 8th entry of the additive-extension precedent (D-01-12 / D-02-2 / D-03-3 / D-04-1 / D-05-9 / D-06-1 / D-12-14 / D-12-X-read-produced-file) lands here; the pattern is now structurally self-evident — future specs inherit the discipline without re-deriving.

### Added (Spec 17 — Data Analysis and Visualisation, Phase 5 + 6 close-out)

> **`persona-core` patch + skill-pack delivery.** Spec 17 is composition-first: one new built-in skill + an additive Spec 12 Protocol amendment (D-12-X-read-produced-file, landed via Spec 17 Phase 4 reopen after the T01 source audit found the bytes-persistence gap V4/V5 verifications didn't trace) + the runtime call site that closes it. The path convention `<workspace>/charts/<id>.png` aligns verbatim with Spec 16's already-locked D-16-5 — zero cross-spec amendment; one charting implementation, two consumers (Spec 17 inline, Spec 16 embedded).

- **`feat`: `data_analysis` built-in skill pack** at [`packages/core/src/persona/skills/builtin/data_analysis/`](packages/core/src/persona/skills/builtin/data_analysis/) — `SKILL.md` (1,587 tokens, under the 2,000-token D-04-7 budget; within R-17-2's 1,400–1,800-token target) teaching load → profile → triage → compute → chart-CHOICE → chart-CLARITY → explain. Three supplements (`styling.md` 1,204 / `large_datasets.md` 1,136 / `chart_families.md` 1,636 tokens) staged on-demand via Spec 16 M1a (D-17-X-supplements-mechanism inherits verbatim). Token-budget regression-guarded at the 1,800-token ceiling (`test_data_analysis_under_budget`); SKILL.md teaches the three-sibling-directory discriminator (`charts/` inline; `uploads/` download; `intermediate/` cross-turn cache) + parquet re-load discipline (D-17-X-intermediate-format, 15× faster than CSV at 1M rows).
- **`feat`: bytes-persistence layer** at [`packages/core/src/persona/sandbox/protocol.py`](packages/core/src/persona/sandbox/protocol.py) + [`local_docker.py`](packages/core/src/persona/sandbox/local_docker.py) + [`packages/api/src/persona_api/sandbox/hosted.py`](packages/api/src/persona_api/sandbox/hosted.py) — additive `CodeSandbox` Protocol amendment per D-12-X-read-produced-file: `copy_produced_file_to(session_id, ref, target_path)` + `read_produced_file_bytes(session_id, ref)` + new `ProducedFileSizeError` (100 MB cap, flows through Spec 06 tool-error-recovery). Local: `shutil.copyfile` (zero-memory disk-to-disk). Hosted: E2B `sandbox.files.read` + `target_path.write_bytes`. **Spec 16 inherits this mechanism** for its eventual docx/pdf/xlsx download path (handover.md updated). The additive-extension precedent (D-01-12 / D-02-2 / D-03-3 / D-04-1 / D-05-9 / D-06-1 / D-12-14) applies.
- **`feat`: runtime call site** at [`packages/core/src/persona/sandbox/tool.py`](packages/core/src/persona/sandbox/tool.py) + [`packages/api/src/persona_api/sandbox/runtime_tool.py`](packages/api/src/persona_api/sandbox/runtime_tool.py) + [`packages/api/src/persona_api/services/runtime_factory.py`](packages/api/src/persona_api/services/runtime_factory.py) + [`packages/api/src/persona_api/app.py`](packages/api/src/persona_api/app.py) per D-17-X-bytes-persistence — `produced_file_persister` injected into `make_code_execution_tool`; the outer `make_pool_code_execution_tool` builds the persister closure (`workspace_root/owner_id/persona_id/<ref>`) + augments the input-files provider to stage `<persona_workspace>/intermediate/*` cross-turn (the SKILL.md's parquet cache pattern survives Spec 12 session reaping). Single composition site; both Spec 05 + Spec 06 loops inherit transparently.
- **`feat`: V4-aligned chart serve surface** — bytes at `<workspace>/<owner_id>/<persona_id>/charts/<id>.png` are served by the existing `GET /v1/personas/:id/uploads/charts/<id>.png` route via `image_service.fetch:300`'s slash-aware ref logic (V4 source verification). Zero route changes, zero service changes. The path-prefix `charts/` IS the inline signal (D-17-X-inline-hint-shape: path-IS-hint; discriminator is `path.split("/")[0]`).
- **`test`: 7 new test files / ~500 LOC across unit + integration** — `test_chart_path_contract.py` (5 unit / cross-spec contract; Spec 16 D-16-5 invariant pinned); `test_large_dataset_triage.py` (9 unit; D-17-4 bucket boundaries + pandas-gated measurement); `test_charts_serve.py` (6 integration; chart serve route + RLS + traversal); `test_spec14_spec17_boundary.py` (4 integration; T01 finding #2 byte-equality across the cross-spec doc-path); `test_stateful_iteration.py` (2 integration; real-Docker D-12-1 filesystem-persists / variable-state-doesn't honesty); `test_magika_fallback_recovery.py` (4 unit; scripted backend + Spec 06 recovery — the v0.1 fallback per D-17-X-magika-deferred-v0.2); `test_chart_quality_bar.py` (5 integration; real-Docker 5-chart-family round-trip + PIL CLARITY surrogates). Plus 7 new tests in [`test_api_sandbox_runtime_tool.py`](packages/api/tests/test_api_sandbox_runtime_tool.py) (T04c bytes-persistence wiring across 3 test classes).

### Known limitations (Spec 17 — production-honest)

- **Chart-CHOICE + chart-CLARITY visual-only dimensions verify when frontend lands.** Criteria #4 + #5 pass on structural surrogates (matplotlib artists; PIL dimensions + colour count + file size) but typography polish, palette aesthetic harmony, and label-placement craft are visual-only and inherit verification from the frontend-readiness state per the project-wide reframe ("test through the backend; frontend isn't ready"). Honest-PARTIAL framing in close-out; NOT a v0.2 deferral.
- **Python variable state does NOT persist across sandbox executes at v0.1** (D-12-1 scaled scope per Spec 12 T05c). Filesystem state persists; variables don't (each `docker exec` is a fresh Python process). The SKILL.md teaches re-load from `intermediate/df.parquet` to work with this. v0.2 lands the IPython-kernel persistent interpreter (D-12-1 long-term path); SKILL.md adapts.
- **Magika file-type detection deferred to v0.2** (D-17-X-magika-deferred-v0.2). v0.1 fallback = extension-trust + parser-raise + Spec 06 tool-error-recovery (T11 verifies the recovery mechanic). Magika's ~50–100 MB ONNX Runtime + NumPy footprint not justified against the 512 MB sandbox ceiling when the recovery mechanism the loops already ship handles the `.xlsx-but-actually-csv` case cleanly.
- **D-15-X-workspace-coordination editorial flag** standing for Spec 15's Phase 6 close-out (NOT Spec 17 to amend) — the "bytes copy out under `uploads/<blake2b>.png`" framing in D-15-X's "Note on Spec 16 D-16-5" paragraph is incorrect for sandbox-produced files per D-12-9 bind-mount semantics.

### Added (Spec 12 — Code Execution Sandbox, Phase 5 + 6 close-out)

- **`feat`: code-execution sandbox — `CodeSandbox` Protocol, `LocalDockerSandbox` (open-source path), `HostedSandbox` (E2B Firecracker microVM), `SandboxPool` (multi-tenant lifecycle + per-user cap + idle reaper), `code_execution` first-class tool, credits hook on dispatch, security-reviewer pass against the live integrated stack.** Closes Spec 12 Phase 5 (T01–T12 with T09 sub-tasked T09a–d) + Phase 6 close-out; **16/16 acceptance criteria met** (with documented SCP-12-4 caveat on §9 #8).
- **`CodeSandbox` Protocol** at [`packages/core/src/persona/sandbox/protocol.py`](packages/core/src/persona/sandbox/protocol.py) — `@runtime_checkable`, explicit `aclose()` per D-12-7. Boundary-crossing types (`ExecutionResult` / `ResourceLimits` / `NetworkPolicy` / `SandboxFile`) at [`result.py`](packages/core/src/persona/sandbox/result.py) are Pydantic v2 frozen with `extra="forbid"` per D-12-14. (T01–T02)
- **`make_code_execution_tool`** at [`packages/core/src/persona/sandbox/tool.py`](packages/core/src/persona/sandbox/tool.py) — Toolbox-ready AsyncTool that the model invokes; `pre_execute_hook` (lazy-eager pool acquire boundary) and `on_execute_success` (credits hook boundary, D-12-3) added at T10. Audit emission per D-12-8 (4 KiB inline + sha256). (T03)
- **`LocalDockerSandbox`** at [`packages/core/src/persona/sandbox/local_docker.py`](packages/core/src/persona/sandbox/local_docker.py) — R-12-2 hardening (15 Docker flags: `cap_drop=ALL`, `read_only`, `network=none`, custom seccomp, `pids_limit`, etc.); D-12-9 two-mount workspace (`/workspace/in` ro + `/workspace/out` rw); kernel-style sessions via `docker exec` (D-12-1 scaled scope: filesystem state persists; variable state v0.2). (T05a–c)
- **`HostedSandbox`** at [`packages/api/src/persona_api/sandbox/hosted.py`](packages/api/src/persona_api/sandbox/hosted.py) — wraps the E2B Code Interpreter SDK (`e2b-code-interpreter>=1.0,<2`, lazy-imported); substrate per D-12-12 (CONFIRMED via all five lock-gates). D-12-13 threat-model separation: substrate-provided isolation only, no R-12-2 replication. (T08)
- **`SandboxPool`** at [`packages/api/src/persona_api/sandbox/pool.py`](packages/api/src/persona_api/sandbox/pool.py) — multi-tenant lifecycle composer; per-user cap `max_per_user=2` (D-12-17); pool-owned `asyncio.Task` reaper at 60s cadence cancelled in `aclose()`; idempotent acquire on `(owner_id, conversation_id)`; structured `event=sandbox_quota_rejection` log telemetry for D-12-17 cap flip-trigger. `SandboxQuotaExceededError → 429` + `SandboxUnavailableError → 503` handlers in [`errors.py`](packages/api/src/persona_api/errors.py). (T09a–d)
- **D-12-17 (mid-Phase-5 warm-pool config sub-decision)** — warm-pool=0 + lazy-eager prewarm + 60s reap + pool-owned reaper task + 5min idle + four env-configurable knobs (`PERSONA_SANDBOX_WARM_POOL_SIZE` / `_REAP_INTERVAL_S` / `_IDLE_TIMEOUT_S` / `_MAX_PER_USER`). Four flip-triggers tied to Spec 11 `turn_logs` telemetry; cost-headroom-not-license-to-over-provision discipline applied.
- **API composition (T10)** — `RuntimeFactory._build_toolbox` adds `code_execution` to the toolbox when `sandbox_pool` is configured; `SandboxRequestContext` contextvar threading via `chat_service.stream_chat` avoids widening `loop_builder` signatures (10+ integration test overrides untouched); D-12-3 flat per-execution credits deduction via `credits_service.deduct(reason="code_execution")` wrapped in `asyncio.to_thread`. (T10)
- **Live E2B Hobby smoke** at [`packages/api/tests/integration/sandbox/test_e2b_pool_smoke.py`](packages/api/tests/integration/sandbox/test_e2b_pool_smoke.py) — `@pytest.mark.external`; 3/3 PASS against live substrate at ~$0.0001 actual spend; per-user cap rejects 3rd acquire without burning substrate budget; reaper task cancels cleanly on aclose. `persona-api[hosted]` extra added. (T09d)
- **Agentic-loop e2e** at [`packages/api/tests/test_api_agentic_e2e_code_execution.py`](packages/api/tests/test_api_agentic_e2e_code_execution.py) — real `AgenticLoop` + real `Toolbox` + real pool + scripted Anthropic-shaped backend; verifies the full T10 wiring layer-by-layer (pool acquire / substrate dispatch / credits hook / round-trip into next step / `RunStatus.COMPLETED`). Composing with T09d's live substrate smoke discharges §9 #14. (T11)
- **T12 multi-perspective adversarial security pass** at [`docs/specs/phase2/spec_12/audit/t12_security_review_2026-06-06.md`](docs/specs/phase2/spec_12/audit/t12_security_review_2026-06-06.md) — workflow `wf_eac7bb76-a3f`: 68 agent calls / 4 perspectives (escape / exfiltration / resource_exhaustion / integration_layer) / 3-vote perspective-diverse verification per finding / synthesizer classification (STRUCTURAL-CLEAR vs ARCHITECTURAL-IMPACT). 2 confirmed HIGHs (both STRUCTURAL-CLEAR; autonomously fixed) + 2 MEDIUMs + 3 LOWs + 13 INFOs + 1 REJECTED (F-T12-ESC-01 recalibrated by 3-vote verify). Substrate cost: $0.0046 vs $0.05 ceiling. (T12)
- **T12 STRUCTURAL-CLEAR fixes** at [`packages/api/src/persona_api/sandbox/hosted.py`](packages/api/src/persona_api/sandbox/hosted.py) + [`context.py`](packages/api/src/persona_api/sandbox/context.py) + [`pool.py`](packages/api/src/persona_api/sandbox/pool.py); regressions at [`packages/api/tests/test_api_sandbox_t12_fixes.py`](packages/api/tests/test_api_sandbox_t12_fixes.py): (a) **F-T12-RES-02** wall_clock_s enforcement via `asyncio.wait_for` at `HostedSandbox.execute` + force-kill stateful session on timeout; (b) **F-T12-RES-01** SCP-12-4 substrate-class limit ceiling documentation + warning log when user-supplied `ResourceLimits` are below E2B Hobby's 2048 MiB memory / 1024 MiB disk floor; (c) **F-T12-INT-01** `:` rejection at both `SandboxRequestContext.__post_init__` (primary boundary) and `SandboxPool._make_session_id` (belt-and-braces guard) — cross-tenant session_id-collision surface structurally removed. 9/9 regression tests green.

### Documentation (Spec 12)

- **SCP-12-1..3 (Phase-5 Gate 3 substrate-class properties)** documented in [`decisions.md`](docs/specs/phase2/spec_12/decisions.md): Firecracker MMDS open-but-empty, OpenSSH on loopback (intra-microVM), 26 intra-sandbox listening ports. Per-microVM scope; not multi-tenant escapes; v0.2 mitigation paths documented.
- **SCP-12-4 (T12 substrate-class limit ceiling)** documented alongside SCP-12-1..3: E2B Hobby substrate enforces minimum 2048 MiB memory / 1024 MiB disk floor; `_create_sandbox` warns at construction when user-supplied limits are below floor (production telemetry surfaces the gap); v0.2 mitigation requires paid E2B tier with custom template.
- **D-12-16 three-principle methodology for adversarial verification against unfamiliar protocols** (Phase-5 Gate 3 close-out): (1) workload decomposition before substrate disqualification; (2) protocol-shape triangulation NOT body-string match; (3) probe scripts surface raw bytes alongside verdicts. Applied throughout T12; the workflow's 3-vote distinct-perspective verifier shape IS the structural defense against plausible-but-wrong findings (process-discipline note in audit doc).
- **D-12-17 close-out audit:** four flip-triggers tied to Spec 11 `turn_logs` telemetry (warm 0→1+ on first-turn p95 > 2.5s; reap 60s→30s on cost+attribution compound; idle 300s→600s+ on session-restart rate > 20% with ≥20-resume minimum-N floor; per-user cap 2→3+ on T09c legitimate-rejection rate ≥5%). Each trigger has explicit measurement gating to prevent speculative reconfiguration.
- **LF-12-3** (project-wide tooling latent finding): `MYPYPATH` workspace-imports require explicit env var pinning; pickup path documented for a future tooling-pass micro-task.
- **LF-12-4** (project-wide workflow-harness latent finding): the Workflow harness's `budget` global only enforces a hard token ceiling when an explicit `+Nk` directive is passed at invocation time; prompt-stated caps are advisory. Distinguish real-money substrate cost (bounded by external billing) from advisory token cost (bounded only by `budget.total`). Documented in [`docs/DECISIONS.md`](docs/DECISIONS.md) Spec 12 latent findings.
- **Phase 6 close-out audit:** [`docs/specs/phase2/spec_12/closeout.md`](docs/specs/phase2/spec_12/closeout.md) with the honest §9 #8 framing — "wall-clock ✅ verified via T12; memory/disk ⚠ documented as SCP-12-4" — preserves the Phase-1 honesty-thread applied to acceptance-table prose rather than laundering substrate-class limitations into false PASSes.

### Added (Spec 14 — Document Ingestion, Phase 5 closing)

- **`feat`: document ingestion — parsers (PDF/DOCX/XLSX/CSV/TXT/MD/code), conversation-scoped DocumentStore, size-aware ingest strategy, PromptBuilder extensions, document upload + lifecycle API.** Closes Spec 14 Phase 5 (T01–T23); §9 criteria #1–#13 pre-checked on disk; external smoke per format (T22b) deferred to Phase 6 close-out per D-11-11 agent/human discipline.
- **`DocumentChunk` sibling schema** at [`packages/core/src/persona/schema/documents.py`](packages/core/src/persona/schema/documents.py) — frozen Pydantic v2, conversation-scoped 4-component chunk IDs (`{conversation_id}::document::{doc_ref}::{index:04d}`). Documents are working material NOT persona identity (Dominant Concern #1 + D-14-X-DocumentChunk-shape). (T02)
- **`DocumentStore`** at [`packages/core/src/persona/stores/document_store.py`](packages/core/src/persona/stores/document_store.py) — conversation-scoped sibling of the four typed stores; composes `Backend` directly per D-14-X-store-shared-base; calling-convention discipline (CSA-1) passes `conversation_id` into the `MemoryStore.write(persona_id, …)` slot per-call. No source-policy axis, no versioning, no decay-rerank — documents are immutable working material. (T03)
- **Criterion-#6 binary no-leak test** at [`packages/core/tests/integration/test_document_store_no_leak.py`](packages/core/tests/integration/test_document_store_no_leak.py) — 7 tests instrumenting the four typed stores' `.write()` methods; representative DocumentStore scenario writes zero typed-store entries. **Stays green for the rest of Phase 5** as the Dominant Concern #1 regression guard. (T04)
- **Document-aware chunker** at [`packages/core/src/persona/documents/chunker.py`](packages/core/src/persona/documents/chunker.py) — natural-boundary first (paragraphs/sections for prose, sheets for spreadsheets, pages for PDFs) with token-aware fallback (D-14-4); 512/64 defaults per R-14-3 + predecessor convention; Phase 1 typed-store chunking byte-for-byte unchanged (regression-asserted). (T05)
- **Five parsers** at [`packages/core/src/persona/documents/parsers/`](packages/core/src/persona/documents/parsers/): `text.py` (txt/md/code language-fenced; T06), `csv.py` (1000-row cap + first/last 50 sample + sandbox pointer; T07), `docx.py` (`python-docx==1.1.2` Spec-12-aligned; T08), `xlsx.py` (`openpyxl==3.1.5` Spec-12-aligned; T09), `pdf.py` (`pypdf` text-extraction + D-14-2 no-text-layer detection `< 50 chars/page`; T10).
- **Parsers dispatcher + lazy-import discipline** at [`packages/core/src/persona/documents/parsers/__init__.py`](packages/core/src/persona/documents/parsers/__init__.py) — `parse_document(path) → ParseResult` with extension-based dispatch; per-parser lazy imports + `MissingDependencyError` with `pip install persona-core[documents]` install hint. Structurally enforced (test_lazy_import_discipline source-grep). 35 supported extensions. (T11)
- **Size-aware ingest strategy** at [`packages/core/src/persona/documents/ingest.py`](packages/core/src/persona/documents/ingest.py) — D-14-1 threshold (3000 tokens; env `PERSONA_DOC_INJECT_THRESHOLD`); three-path decision (`WHOLE_INJECT` / `RETRIEVAL` / `VISION_HANDOFF`); the load-bearing D-14-1 sub-decision "threshold drops, ladder doesn't rearrange" is encoded. (T12)
- **`document_service.upload`** at [`packages/api/src/persona_api/services/document_service.py`](packages/api/src/persona_api/services/document_service.py) — workspace+sidecar layout under `resolve_sandbox_path`; `DocumentRef` API-boundary type; `remove_all_for_conversation` is the cascade-helper T19 reuses (D-14-X-cascade-coordination). CSA-2 dispatcher-compatible upload signature (conversation-scoped; differs from Spec 13's persona-scoped `image_service.upload`, both fit one content-type dispatcher). (T13)
- **`PromptBuilder` extensions** at [`packages/runtime/src/persona_runtime/prompt.py`](packages/runtime/src/persona_runtime/prompt.py): `DocumentInjection` + `DocumentContext` siblings of `RetrievedContext` (T14); retrieved chunks render ABOVE episodic only when retrieval non-empty per D-14-5 conservative rule (T15); **"what's in scope" synopsis (T16) — Dominant Concern #2 structural defence** — present every turn under retrieval, lists ALL attached documents regardless of retrieval. Section ordering: identity → constraints → self-facts → worldview → synopsis → retrieved-chunks → episodic → skill-index → active-skill → whole-inject-docs → footer. Reduction ladder extended to 4 stages with documents present (drop AFTER episodic, BEFORE worldview). (T14, T15, T16)
- **`routes/uploads.py` content-type dispatch** at [`packages/api/src/persona_api/routes/uploads.py`](packages/api/src/persona_api/routes/uploads.py) — CSA-2 dispatcher: `image/*` → `image_service.upload` (Spec 13); document MIME types → `document_service.upload` (Spec 14) with required `conversation_id` form field. Unknown formats → 415. (T17)
- **`routes/documents.py`** at [`packages/api/src/persona_api/routes/documents.py`](packages/api/src/persona_api/routes/documents.py) — `GET /v1/conversations/:id/documents` (list) + `DELETE /v1/conversations/:id/documents/:ref` (per-document deletion). RLS-scoped via `chat_service.get_conversation` (404 if cross-tenant). (T18)
- **Conversation cascade-delete extension** at [`packages/api/src/persona_api/routes/conversations.py`](packages/api/src/persona_api/routes/conversations.py) — `DELETE /v1/conversations/:id` now cascade-cleans document workspace files + DocumentStore chunks via `document_service.remove_all_for_conversation`. Co-landing-ready with Spec 13's T12 image-cascade extension per D-14-X-cascade-coordination. **Criterion #6 re-asserted at the cascade boundary** ([`test_api_conversation_cascade.py::TestCriterion6HoldsAtCascadeBoundary`](packages/api/tests/test_api_conversation_cascade.py)). (T19)
- **Bounded-prompt-tokens regression test** at [`packages/api/tests/integration/test_document_prompt_bound.py`](packages/api/tests/integration/test_document_prompt_bound.py) — 50-page document × 5-turn scenario; max prompt < **D-14-X-prompt-bound-target = 30 000 tokens** (~45% headroom over Spec 11's empirical `max_prompt_tokens=20553`); per-turn spread < 2000 tokens (proves bounded-not-cumulative); synopsis present every turn. **Dominant Concern #2 regression guard.** (T20)
- **Scanned-PDF vision handoff (criterion #7)** in `document_service.upload` — when `parse_result.needs_vision_handoff=True`, rasterise pages via `pypdfium2` (BSD/Apache-2.0 per D-14-X-pdf-library-license) at 150 DPI (env `PERSONA_DOC_PDF_RASTER_DPI`, range 100–300), persist as PNGs under workspace, return `DocumentRef.images` with Spec 13 `ImageContent` references for runtime vision-tier routing (D-13-X-pdf-contract). The interim `VisionHandoffRequiredError` class + the `TODO(T21)` catch-block in `routes/uploads.py` have been **removed** per the close-out discipline. (T21)
- **Cross-tenant RLS sweep** at [`packages/api/tests/integration/test_documents_rls.py`](packages/api/tests/integration/test_documents_rls.py) — 7 binary assertions across POST upload / GET list / DELETE / cross-tenant conversation-delete; existence-disclosure-safe 404 (criterion #13). (T22a)
- **Per-format `@pytest.mark.external` smoke scaffold** at [`packages/api/tests/external/test_documents_smoke.py`](packages/api/tests/external/test_documents_smoke.py) — 8 scenarios (txt / md / csv / docx / xlsx / text-PDF / scanned-PDF-vision / code) ready for the operator close-out checklist per D-11-11 agent/human discipline. (T22b)
- **`[documents]` extra parser libs as dev deps** at [`pyproject.toml`](pyproject.toml) — `pypdf>=6.0,<7`, `pypdfium2>=5.0,<6`, `python-docx>=1.1,<2`, `openpyxl>=3.1,<4`. D-14-X-documents-extra deferred to v0.2 (lazy-import + `MissingDependencyError` discipline IS the structural defence regardless).

### Documentation (Spec 14)

- **Phase 4 architectural-rule sibling** — D-14-X-pdf-library-license codifies the license-stack rule across `persona-core` / `persona-api` / `persona-web` (sibling of D-13-X-pillow). `pymupdf` hard-rejected for AGPL incompatibility. Marked `[architectural-rule] [project-wide]` in [`docs/DECISIONS.md`](docs/DECISIONS.md).
- **CSA-1 + CSA-2** — Scope binding to single-Protocol stores (Spec 14 D-14-X-scope-binding-discipline) + Cross-spec upload-route extension (Spec 13 T11 + Spec 14 T17). Recorded as project-wide architectural rules in [`docs/DECISIONS.md`](docs/DECISIONS.md).
- **D-14-X-workspace-sidecar-v0.2-promotion** — sibling of D-14-X-documents-extra v0.2 note; records the workspace+sidecar deferral so the v0.2 maintainer doesn't rediscover the choice. Pattern: v0.1 ships workspace+sidecar; v0.2 promotes to `conversations.documents JSONB` when API surface warrants the migration cost.

### Decisions (Spec 14)

D-14-X-spec-13-coordination (Option A — sequence around Spec 13 T03); D-14-X-uploads-coordination (Option A — content-type dispatch in shared `routes/uploads.py`); D-14-X-cascade-coordination (one DELETE-handler refactor); **D-14-X-scope-binding-discipline** (calling-convention path (a)); D-14-X-no-source-policy-on-documents; D-14-X-store-shared-base (skip refactor); D-14-X-DocumentChunk-shape (sibling); D-14-X-document-chunk-id (4-component); D-14-X-document-store-divergence-from-episodic (sibling, no decay); **D-14-1 = 3000 tokens** (threshold drops if conflict, ladder doesn't rearrange); D-14-X-prompt-bound-target = 30 000; D-14-2 (`pypdf` + `< 50 chars/page` + `pypdfium2` at 150 DPI); D-14-3 (1000-row cap + first/last 50); D-14-4 (document-aware chunker); D-14-5 (whole-conversation persistence + above-episodic-only-when-retrieved rank); D-14-X-pptx-deferral; D-14-X-synopsis-source (auto-generated, no caching); D-14-X-documents-extra (deferred v0.2); D-14-X-spec-13-T11-gating; **D-14-X-pdf-library-license** (project-wide license-stack rule). All in [`docs/specs/phase2/spec_14/decisions.md`](docs/specs/phase2/spec_14/decisions.md), mirrored to [`docs/DECISIONS.md`](docs/DECISIONS.md).

### Added (Spec 16 — Document Generation Skills, Phase 5 + 6 close-out)

> **`persona-core 0.X.0` candidate.** Four built-in SKILL.md packs that teach the persona to produce real downloadable Word / PowerPoint / Excel / PDF files by writing code in the Spec 12 sandbox. The architecture was chosen as composition, not construction: no `DocumentService`; no new tool; the existing `code_execution` tool runs python-docx / python-pptx / openpyxl / reportlab code from the SKILL.md guidance, and the existing produced-files contract returns the bytes. Acceptance audit **11 / 11 PASS** with 9 PARTIAL classified as visual-only surrogates or honest production constraints — zero (c) skill-authoring gaps; zero FAIL.

- Four built-in `persona-core` skill packs at [`packages/core/src/persona/skills/builtin/`](packages/core/src/persona/skills/builtin/): `docx_generation` (1657 tok), `pptx_generation` (1738 tok), `xlsx_generation` (1822 tok), `pdf_generation` (1860 tok). All ≤ 2000-token D-04-7 ceiling. Each ships a lean SKILL.md + on-demand `supplements/*.md` (4 + 3 + 3 + 3 = 13 supplements covering verbose API detail under the M1a runtime affordance).
- M1a runtime affordance — `persona.skills.collect_skill_supplements(spec)` helper at [`packages/core/src/persona/skills/use_skill_tool.py`](packages/core/src/persona/skills/use_skill_tool.py); `ConversationLoop.deferred_input_files` / `AgenticLoop.deferred_input_files` public attributes (D-16-2-state-location option (a)); `make_code_execution_tool(..., deferred_input_files_provider: Callable[[], list[SandboxFile]] | None = None)`; api composition-root wiring at [`packages/api/src/persona_api/sandbox/runtime_tool.py`](packages/api/src/persona_api/sandbox/runtime_tool.py) + [`runtime_factory.py`](packages/api/src/persona_api/services/runtime_factory.py). 58 NEW LOC under the 60-LOC D-16-2-fallback-trigger budget; M0 contingency never fired.
- **D-16-4 ACTIVATED** — all four SKILL.md packs reference `persona.identity.visual_style` (Spec 15 T10 shipped the field mid-Phase-5; HTML-comment fallback never used; closed-loop with Spec 15 close-out).
- Backend integration test suite (criterion #2 binary quality test): `test_docx_generation_e2e.py`, `test_pptx_generation_e2e.py`, `test_xlsx_generation_e2e.py`, `test_pdf_generation_e2e.py` (T11–T14, real LocalDockerSandbox + real on-disk SKILL.md + real M1a supplements + parses produced file via python-docx / python-pptx / openpyxl / pypdf and asserts research.md §3.7 surfaces) + `test_document_generation.py` (T09 allow-list + composition) + `test_document_generation_loops.py` (T10 both-loops + RLS + error recovery).
- `docker` pytest marker registered in root `pyproject.toml`; `pypdf>=6.0,<7` added to `packages/core/pyproject.toml` `[project.optional-dependencies].test`; `python-pptx>=1.0,<2` added to root `[dependency-groups].dev` for host-side §3.7 asserts.
- Four real inspection artifacts at [`docs/specs/phase2/spec_16/inspection/`](docs/specs/phase2/spec_16/inspection/) (gitignored per D-16-X-3; reproducible by re-running T11–T14): 39,710 / 62,999 / 6,578 / 44,269 bytes.

### Added (Spec 12 additive amendment surfaced by Spec 16 Phase 5b)

- **D-12-X-venv-path-ordering** — `LocalDockerSandbox._BASE_CONTAINER_KWARGS["environment"]["PATH"]` prepends `/opt/venv/bin` so image-installed venv tooling (`from docx import Document` / `openpyxl` / `python-pptx` / `reportlab`) resolves natively from `python`/`pip` inside the running container. R-12-2 explicit-PATH hardening intent preserved (no shell-injection vector). **Production fix, not v0.2 deferral.** Test-only `_VENV_PRELUDE` workaround removed from T09 + T10 (verifying-the-fix-by-removing-the-workaround discipline). Cross-link: Spec 16 D-16-X-6.

### Fixed (Spec 16 — production bugs surfaced during Phase 5)

- **D-16-2-supplements-relative-path** (= D-16-X-7) — `persona.skills.collect_skill_supplements` now emits relative `.skills/<name>/supplements/<topic>.md` paths in the `SandboxFile.path` transport field instead of the pre-fix absolute `/workspace/in/.skills/...` paths. Pre-fix, `Path(host_in) / Path('/workspace/in/...')` short-circuited per Python's `Path('/x') / '/y' == Path('/y')` semantics; the host-side write raised `OSError: [Errno 30] Read-only file system: '/workspace'` on macOS, and production `use_skill` activation silently failed → SKILL.md "read `/workspace/in/.skills/<name>/supplements/<topic>.md`" teaching raised `FileNotFoundError`. The SKILL.md packs continue to teach the absolute model-facing path (correct inside the container view); the fix re-aligns the producer with the `SandboxFile.path` "relative to the workspace root. Never absolute." contract. Source-of-truth discipline: path-in-transport is relative; absolute is only the mounted destination. **Production fix, not maintenance-pass deferral.**

### Inherited (Spec 17 cross-spec — closed-loop with Spec 17 Phase 4 reopen)

- **D-12-X-read-produced-file** + **D-17-X-bytes-persistence** — bytes-from-sandbox → API-workspace path landed by Spec 17. Spec 16 docx/pptx/xlsx/pdf files surface via the same `format_tool_result` convergence Spec 17 uses for charts. **No per-loop wiring on Spec 16's side** — the runtime `_persist_produced_file` callback at [`packages/api/src/persona_api/sandbox/runtime_tool.py:216`](packages/api/src/persona_api/sandbox/runtime_tool.py#L216) iterates `produced_files` and persists each to `<workspace_root>/<owner_id>/<persona_id>/<filename>`. T11–T14 assert at the persona-workspace path.

### Documentation (Spec 16)

- Full spec lifecycle in [`docs/specs/phase2/spec_16/`](docs/specs/phase2/spec_16/): `spec_16_kickoff.md`, `spec_16_document_generation.md`, `research.md` (R-16-1..5 + §0 precursor verifications + §3 quality bar + §3.7 per-criterion test surfaces), `decisions.md` (5 primaries + 5 sub-decisions + 4 micros + §13 Phase 5b additions), `state.md` (Phase 5 task log + per-format scorecards + Phase 5 final tally), `handover.md` (orientation pack + cross-spec inheritance section), `closeout.md` (11-criterion audit + risk #1 classification + v0.2-candidates table + cross-spec ledger + CHANGELOG plan).
- Forward-reference [`docs/specs/phase2/spec_17/contract_inherited_from_spec_16.md`](docs/specs/phase2/spec_17/contract_inherited_from_spec_16.md) — Spec 17 inherits the PNG-at-`<workspace>/charts/<id>.png` chart-embed contract; D-16-X-5 same-session-only constraint folded into all four format supplements.

### Production hardening — landed pre-close-out (NOT v0.2-deferral)

- **D-16-X-6** / D-12-X-venv-path-ordering (PATH fix) — production fix, landed in Phase 5b.
- **D-16-X-7** / D-16-2-supplements-relative-path (`collect_skill_supplements` relative-path fix) — production fix, landed in Phase 5b.
- 5 regression tests on the supplements relative-path round-trip + 2 path-ordering regression tests guard the fixes against future drift.

### Pure v0.2 scope-boundary items (NOT production-hardening)

- **D-16-X-1** — image library pin lag (reportlab 4.2.5 vs 4.5.1; python-docx 1.1.2 vs 1.2.0; 3–6 months behind latest as of 2026-06-06). **Not blocking v0.1** — all four pinned versions ship the §3 quality-bar features the scorecards verified. Next Spec 12 image rebuild bumps in lockstep. **Undated** entry candidate for the MAINTENANCE.md proposal (event-driven, not date-driven).

### Decisions (Spec 16)

- **D-16-1..5** + sub-decisions D-16-2-wiring / D-16-2-path / D-16-2-fallback-trigger / D-16-2-rejection-M2 / D-16-2-state-location / D-16-2-supplements-relative-path / D-16-5-rejection-SVG; **D-16-X-1..7** micros. All entries in [`docs/specs/phase2/spec_16/decisions.md`](docs/specs/phase2/spec_16/decisions.md) + one-liners at [`docs/DECISIONS.md`](docs/DECISIONS.md) Spec 16 section. Acceptance: **11 / 11 PASS** (zero FAIL); 9 PARTIAL all classify as visual-only surrogates or honest production constraints per closeout.md §2. **CLOSED 2026-06-06.**

## [0.15.0] — 2026-06-06

> Spec 15 — Image Generation. Phase 6 close-out signed off 2026-06-06. Three Phase 6 corrections folded in: D-15-3 production flip `count <= 2` → `count <= 4`; D-15-X-workspace-coordination phrasing correction (Spec 15 bytes flow **provider API → workspace direct**, not via sandbox — Spec 16/17's sandbox-copy mechanism is a different decision pair); D-15-X-hard-line-filter lexicon review locked as calendar-bound production maintenance (first review 2026-12-06, aligned with EU AI Act Article 5 amendment effective date). §9 acceptance audit: 13/13 ✅ in-CI + 5/13 🟦 LIVE-half operator-passed. Default test suite: **2,309 passed, 312 deselected**. `mypy --strict` clean on core+runtime (114 files); `mypy` clean on api (52 files); `ruff check` + `ruff format --check` clean on Spec 15 surface (33 files). Full close-out audit at [`docs/specs/phase2/spec_15/closeout.md`](docs/specs/phase2/spec_15/closeout.md).
>
> **Calendar-bound operator commitments entered at this release** (not deferred features): 2026-09-01 EU AI Act amendment status check; 2026-10-01 OpenAI gpt-image-1 deprecation watch; 2026-12-06 first lexicon review (six-month cadence thereafter).

### Added (Spec 15 — Image Generation)

- **`feat`: text-to-image generation — `ImageBackend` Protocol + OpenAI gpt-image-1 + Flux 1.1 [pro] via fal.ai backends, `generate_image` first-class AsyncTool with three-layer safety (provider moderation + persona constraints + categorical hard-line filter), pre-deduct-credits + per-user advisory-lock cap=1 cost discipline, persona `identity.visual_style` additive schema extension, `POST /v1/personas/:id/imagegen` route reusing Spec 13's workspace + GET serve surface.** Closes Spec 15 Phase 5 (T01–T21); §9 criteria #1–#13 pre-checked on disk; T19 visual-style + T20 provider smoke live half deferred to Phase 6 close-out per D-11-11 agent/human discipline.
- **`ImageBackend` Protocol** at [`packages/core/src/persona/imagegen/protocol.py`](packages/core/src/persona/imagegen/protocol.py) — `@runtime_checkable`, mirrors Spec 02's `ChatBackend` shape (provider_name/model_name properties + async `generate` + reserved `edit` raising `NotImplementedError("edit not supported in v1")` per D-15-X-edit-protocol-reservation). Boundary-crossing types at [`result.py`](packages/core/src/persona/imagegen/result.py) are Pydantic v2 frozen with `extra="forbid"` per D-15-X-pydantic-boundary-types (six-spec precedent: D-01-12 / D-02-2 / D-03-3 / D-05-9 / D-06-1 / D-12-14 / D-13-X-now corrects the spec §4 `@dataclass` sketches). `count: int = Field(ge=1, le=4)` per D-15-3 + LF-13-2 (Phase 6 production flip from `le=2` — at pre-deduct + advisory-lock cap=1 the cost is bounded $0.16–$0.668/call OpenAI medium→high and $0.16 fal flat; the parallel-fire T17 structural proof holds regardless of count). (T03, T04)
- **`ImageBackendConfig`** at [`packages/core/src/persona/imagegen/config.py`](packages/core/src/persona/imagegen/config.py) — `BaseSettings` reading `PERSONA_IMAGEGEN_*` env vars mirroring Spec 02's `BackendConfig.from_env` shape; `SecretStr` credential discipline; `fal_safety_tolerance: int = Field(default=2, ge=1, le=6)` per D-15-X-provider-moderation-default. Missing api_key returns `None` at config-time; concrete backend constructors raise `ImageGenUnavailableError` at `__init__` (fail-fast at composition root). (T04)
- **Four flat domain exceptions** at [`packages/core/src/persona/imagegen/errors.py`](packages/core/src/persona/imagegen/errors.py): `ImageGenError(PersonaError)` base + `ImageGenUnavailableError` (missing/invalid creds) + `ImageProviderError` (rate limit / transient / model_not_found / timeout / unsupported_option) + `ContentRejectedError(reason, stage)` (three call sites — categorical hard-line filter, provider input moderation, provider output moderation). All accept structured `context: dict[str, str]` per CLAUDE.md domain-exception discipline. (T02)
- **`load_image_backend` factory** at [`packages/core/src/persona/imagegen/_factory.py`](packages/core/src/persona/imagegen/_factory.py) — dispatches on `config.provider` to `OpenAIImageBackend` / `FalImageBackend` via lazy-imports inside the function body (`persona.imagegen` stays importable before either concrete backend's SDK deps land). Mirrors `persona.backends._factory.load_backend`. Unknown providers raise `ImageProviderError` with `context={"provider": ..., "supported": "openai, fal"}`. (T05)
- **`OpenAIImageBackend`** at [`packages/core/src/persona/imagegen/openai_image.py`](packages/core/src/persona/imagegen/openai_image.py) — uses existing `openai.AsyncOpenAI` (Spec 02 dep; no new dep). Co-located `_OPENAI_IMAGE_CAPABILITY` matrix near top of file mirroring `openai_compat.py:72-121`. **Size mapping (D-15-X-size-rounding):** `"1024x1792" → "1024x1536"`, `"1792x1024" → "1536x1024"`; the **requested** size is preserved in the audit metadata. **Quality mapping:** `"standard" → "medium"`, `"high" → "high"`. Adapter-boundary error mapping: `openai.AuthenticationError → ImageGenUnavailableError`, `openai.RateLimitError → ImageProviderError(reason="rate_limit")`, `openai.BadRequestError(moderation_blocked) → ContentRejectedError(reason="provider_moderation", stage="input"|"output")`, `openai.NotFoundError → ImageProviderError(reason="model_not_found")`, `openai.APITimeoutError → ImageProviderError(reason="timeout")`. (T06)
- **`FalImageBackend`** at [`packages/core/src/persona/imagegen/fal_image.py`](packages/core/src/persona/imagegen/fal_image.py) — wraps `fal-client>=1.0,<2` (new dep declared in [`packages/core/pyproject.toml`](packages/core/pyproject.toml); Apache-2.0 per D-15-X-license-stack). Co-located `_FAL_IMAGE_CAPABILITY` matrix. **Size mapping:** custom dims passed through (fal accepts arbitrary width/height). **Quality mapping:** no-op + debug log (Flux 1.1 [pro] has no quality dial). **Safety tolerance:** `safety_tolerance=str(config.fal_safety_tolerance)` default `"2"` per D-15-X-provider-moderation-default. **D-15-X-flagged-image-policy:** if `has_nsfw_concepts[i] = true` for ANY image → raise `ContentRejectedError(reason="provider_post_gen_moderation", stage="output")`. CDN URL bytes downloaded via httpx into memory (no two-trip pattern). Adapter-boundary error mapping mirrors OpenAI shape (Auth → unavailable, RateLimit → rate_limit, HTTP 422 content-policy → `ContentRejectedError(reason="provider_moderation", stage="input")`, FalServerException 5xx → transient). (T07)
- **Provider-agnostic contract test suite** at [`packages/core/tests/unit/imagegen/test_contract.py`](packages/core/tests/unit/imagegen/test_contract.py) — 10 contract assertions × 2 backends = 22 parametrised cases including the **binary symmetry test** (#9): unsupported `(model, size)` pair raises `ImageProviderError(reason="unsupported_option")` on BOTH providers BEFORE the SDK boundary is reached (OpenAI capability matrix's `frozenset()` fallback; fal matrix monkeypatched with empty frozenset). Verifies the unified shape is real, not just shared property names. (T08)
- **Categorical hard-line filter** at [`packages/core/src/persona/imagegen/safety.py`](packages/core/src/persona/imagegen/safety.py) — **adversarial-tests-first per Spec 12 T12 / Spec 03 sandbox precedent**: 60-case closed-helper corpus (`_build_corpus()` generates 6 buckets × 10 cases — B1 C1 conservative positives, B2 C2 numeric-age positives, B3 C3 developmental-stage positives, B4 obfuscation positives, B5 accepted-false-positive zone, B6 lexical-overlap-only negatives) constructed at test-execution time from closed `_T_MINOR_SET` / `_T_DEVELOPMENTAL_SET` / `_T_SEX_SET` frozensets inside the test file — harmful surface area NEVER committed as standalone phrases. The shipped filter lands `normalise` (NFKD + combining-mark strip + zero-width strip + lowercase + confusable-fold + leet-fold + `c.h.i.l.d` / `c h i l d` collapse), `leet_fold_inside_alpha` (calibrated: only when an alpha character neighbours the digit within the same word-run), `tokenise` (Unicode-aware `\W+` via the new `regex>=2024.0,<2027` dep), `is_hard_line_violation(prompt) → tuple[bool, "c1"|"c2"|"c3"|None]` with priority order C3 (developmental ∩ sex) → C1 (minor ∩ sex) → C2 (numeric-age 0-17 within 8-token window of sex token on ORIGINAL pre-leet-fold tokens), and `hash_prompt_for_audit` (sha256 hex — **the only thing ever persisted about a triggering prompt**). (T09)
- **`identity.visual_style` additive schema extension** at [`packages/core/src/persona/schema/persona.py`](packages/core/src/persona/schema/persona.py) — `visual_style: str | None = None` added per D-15-4 / D-01-12 additive-extension pattern. Regression-asserted: every shipped valid persona fixture round-trips byte-for-byte with `visual_style is None`; raw YAML never carries the field; `extra="forbid"` still rejects typos (`viual_style → ValidationError`). Unblocks Spec 16 D-16-4 (SKILL.md bodies may now reference `persona.identity.visual_style`). (T10)
- **`merge_visual_style`** at [`packages/core/src/persona/imagegen/_merge.py`](packages/core/src/persona/imagegen/_merge.py) — `f"{prompt}, in the style of {style}"` suffix-conditioning template per D-15-4; `_user_specified_style(prompt)` short-circuits to identity (user wins) via three deterministic heuristics: (a) substring `"in the style of"`, (b) `"as a <modifier> <painting|sketch|render|illustration|drawing|photo>"` window detection, (c) tail-position adjective from the closed 21-entry `_KNOWN_STYLE_TAIL` frozenset (both `watercolour` UK + `watercolor` US listed). Deterministic mechanics only — model-behaviour assertions ("watercolour cat is a recognisable cat") are T19's `@pytest.mark.external` burden. (T11)
- **`make_generate_image_tool` AsyncTool factory** at [`packages/core/src/persona/imagegen/tool.py`](packages/core/src/persona/imagegen/tool.py) — composes the four safety primitives: (1) `is_hard_line_violation` pre-dispatch BEFORE any backend call → content-hash-only `ToolAuditEvent(metadata={"outcome":"content_rejected_hard_line", "category": "c1|c2|c3", "prompt_sha256": ...})` + structured failure `ToolResult`; (2) `merge_visual_style(prompt, persona_visual_style)`; (3) `ImageGenOptions` validation (D-15-3 `count <= 4` cap); (4) `await backend.generate(merged_prompt, options=options)` inside a two-arm `except ContentRejectedError / except ImageGenError` funnel emitting the appropriate outcome string. The four outcome strings (`ok` / `content_rejected_hard_line` / `content_rejected_provider` / `error`) all land in `ToolAuditEvent.metadata["outcome"]` per D-15-X-audit-event-extension. Toolbox allow-list integration verified via real `Toolbox` (not a fake): a persona whose `tools` allow-list omits `generate_image` raises `ToolNotAllowedError` at `toolbox.dispatch(...)` (§9 criterion #4 binary structural test). The hard-line audit explicitly asserts the trigger text never appears anywhere in `event.model_dump_json()` — the "NEVER persist the prompt" discipline from D-15-X-hard-line-filter is structurally enforced, not commented. (T12)
- **`credits_service.refund`** at [`packages/api/src/persona_api/services/credits_service.py`](packages/api/src/persona_api/services/credits_service.py) — reverse-deduct ledger entry per D-15-X-credit-flow-semantics pattern (a): single transaction writes `credit_transactions(delta=+amount, reason="image_gen_refund:...")` + `UPDATE credits SET balance = balance + amount`. The T01 source audit confirms `credit_transactions.delta` is `Integer, nullable=False` with **no CheckConstraint** — positive deltas physically allowed; **no Alembic migration required**. (T13)
- **Per-user advisory-lock cap=1** at [`packages/api/src/persona_api/imagegen/concurrency.py`](packages/api/src/persona_api/imagegen/concurrency.py) — `pg_try_advisory_xact_lock(('x' || md5(:user_id))::bit(64)::bigint)` inside `rls_engine.begin()` per D-15-X-concurrency-cap. Multi-worker-correct from day one (async-semaphore rejected — in-process state doesn't survive multi-worker deploys); auto-released on transaction commit/rollback. Returns `None` on failure → `ConcurrencyCappedError` → 429 + `Retry-After`. (T14)
- **`persona_api.imagegen.service.generate`** at [`packages/api/src/persona_api/imagegen/service.py`](packages/api/src/persona_api/imagegen/service.py) — composition root: cap acquisition + `await backend.generate(...)` live INSIDE one `rls_engine.begin()` block so the advisory lock holds for the full provider latency. Pre-deduct credits BEFORE the backend call per D-15-X-pre-deduct-credits (denial-of-wallet under parallel fire is structurally impossible iff credits are atomic-acquired BEFORE the provider call). On `ContentRejectedError` / `ImageGenError`: outer txn rolls back (releasing the lock), FRESH `credits_service.refund(...)` transaction issues the reverse-deduct ledger entry — ledger captures both legs as `[-100, +100]`. Bytes persisted at D-13-4 layout `{workspace_root}/{owner_id}/{persona_id}/uploads/<blake2b>.<ext>` via `resolve_sandbox_path` + `O_NOFOLLOW` write per D-15-X-workspace-coordination — **the existing `GET /v1/personas/:id/uploads/:ref` route serves them provenance-blindly. No new GET route. No new workspace layout. No CSA-2 dispatcher fork.** (T15)
- **`POST /v1/personas/:id/imagegen` route** at [`packages/api/src/persona_api/routes/imagegen.py`](packages/api/src/persona_api/routes/imagegen.py) + startup wiring in [`packages/api/src/persona_api/app.py`](packages/api/src/persona_api/app.py) — auth + pre-flight RLS persona check (404 cross-tenant) + credits pre-flight gate (402 when exhausted) + service-layer dispatch + API-layer `audit_service.record(action="imagegen.create", ...)` capturing the **requested** size (not the OpenAI-rounded value) per D-15-X-size-rounding. Two-audit-emission discipline (Phase 4 fold-in): T12 emits the persona-layer `ToolAuditEvent` ("what did this persona do?"); T16 emits the API-layer record ("what hit this endpoint?") — same pattern as Spec 13 uploads. Domain-exception funnel: `ConcurrencyCappedError → 429 + Retry-After`, `ImageGenUnavailableError → 503 + Retry-After` (new handler in `errors.py`), `ContentRejectedError → 422` with structured `content_rejected` body carrying `reason`/`stage`, `ImageProviderError → 502`. (T16)
- **Parallel-fire regression test** at [`packages/api/tests/integration/test_imagegen_parallel_fire.py`](packages/api/tests/integration/test_imagegen_parallel_fire.py) — **binary structural proof of D-15-X-pre-deduct-credits + D-15-X-concurrency-cap *combined***. 10 concurrent `POST /v1/personas/:id/imagegen` from one user against a `_SlowBackend` holding ~500ms; five binary invariants assert the structural property tight: HTTP status distribution = exactly `1×201 + 9×429`; `backend.call_count == 1` (cap blocked the amplifier); credits ledger has exactly one `-100` deduct (no parallel deducts, no refund-pair); `final_balance == start_balance - 100` exactly; audit log has exactly one `imagegen.create` row for this user; exactly one file under `uploads/`. Removing either lock breaks the test; both removed = denial-of-wallet amplifier. (T17)
- **Cross-tenant RLS sweep** at [`packages/api/tests/integration/test_rls_per_endpoint.py`](packages/api/tests/integration/test_rls_per_endpoint.py) — single test extending the Spec 13 T11 uploads pair shape: B's `POST /v1/personas/:A_persona_id/imagegen` returns 404 (existence-disclosure-safe per D-08-1) AND three Spec-15-specific side-effect invariants hold: no bytes written for B, no credits movement for B, no `imagegen.create` audit row for B. Wired with `_NoOpImageBackend` whose `generate()` raises `AssertionError` if reached — so if a regression ever caused the 404 to fire AFTER backend dispatch instead of BEFORE, the test surfaces it loudly. (T18)
- **Visual-style empirical smoke scaffold** at [`packages/api/tests/external/test_imagegen_visual_style.py`](packages/api/tests/external/test_imagegen_visual_style.py) — 16 parametrised cases (8 cases × 2 providers) per research.md §3.5, including the **criterion #6 conflict case** (dark-moody persona + cheerful birthday card → cheerful wins), the **explicit-user-override case** ("a cat in the style of Van Gogh" with dark-moody persona → Van Gogh cat), and the **Norwegian descriptor case** ("akvarell, dempete farger" + cat → watercolour aesthetic). `@pytest.mark.external`; env-gated SKIP without `OPENAI_API_KEY` AND `FAL_KEY`; bytes written to `tmp_path` + JSON-lines manifest for operator walk; PNG/JPEG magic-prefix assertion proves bytes are real images not error envelopes. Operator-driven per D-11-11. (T19)
- **Live provider smoke matrix scaffold** at [`packages/api/tests/external/test_imagegen_smoke.py`](packages/api/tests/external/test_imagegen_smoke.py) — 4-cell matrix: `[openai, fal] × [happy_path, moderation_trigger]`. The moderation cell uses adult-sexual-content prompts (deliberately outside T09's hard-line categorical zone — no minor/non-consensual tokens) and asserts both backend surfaces surface as `ContentRejectedError` with `context["reason"] in {"provider_moderation", "provider_post_gen_moderation"}`. **Hard-line categorical refusal is NOT live-tested** — T09 owns it; sending a CSAM/NCII prompt to a third-party provider would be the very harm the filter exists to prevent. `@pytest.mark.external`; SKIP-on-missing-key. Operator-driven per D-11-11. (T20)
- **New deps** — `fal-client>=1.0,<2` (Apache-2.0) added to [`packages/core/pyproject.toml`](packages/core/pyproject.toml) for the fal.ai backend; `regex>=2024.0,<2027` added for Unicode-aware `\W+` tokenisation in the hard-line safety filter. Both with mypy overrides in root [`pyproject.toml`](pyproject.toml) (no `py.typed` markers in either package, mirroring the docker/pgvector/jose/e2b/openpyxl/pypdfium2 discipline). License-stack discipline per D-15-X-license-stack.

### Documentation (Spec 15)

- **Workspace-coordination invariant (Phase 6 phrasing-corrected)** — Spec 13 (arbitrary uploads at `uploads/<ref>`) + Spec 15 (generated images at `uploads/<blake2b>.<ext>`) + Spec 16/17 (charts at `uploads/charts/<id>.png`) share the `uploads/` workspace prefix; each owns its sub-convention; service prose for one must NOT cross-contaminate the others' paths. **Critical compositional detail:** Spec 15's bytes flow **provider API → `image_service.generate` → workspace direct** (bytes NEVER traverse the sandbox); Spec 16/17's bytes flow **sandbox `/workspace/out/charts/<id>.png` → copy out to persona workspace** per D-12-X-read-produced-file + D-17-X-bytes-persistence. The `uploads/` directory is **provenance-blind at the filesystem layer**; provenance lives in audit (`ToolAuditEvent.metadata`) + per-turn observability (`turn_logs.metadata` with `kind=image_generation` per D-15-X-observability-shape). Forking `generated/` or `charts/` as a sibling would require GET-route forking + cascade-delete duplication + an operator mental-model split, which D-15-X-workspace-coordination explicitly rejects. Recorded in [`docs/specs/phase2/spec_15/state.md`](docs/specs/phase2/spec_15/state.md) "Workspace-coordination invariant" block.
- **Two-audit-emission discipline** — T12 emits the persona-layer `ToolAuditEvent` ("what did this persona do?"); T16 emits the API-layer `audit_service.record(action="imagegen.create", ...)` ("what hit this endpoint?"). Both deliberate, same shape as Spec 13 uploads. Documented in [`docs/specs/phase2/spec_15/decisions.md`](docs/specs/phase2/spec_15/decisions.md) D-15-X-audit-event-extension.

### Known limitations (Spec 15, v0.1)

- **Count cap `count <= 4`** per D-15-3 (Phase 6 production-flipped from the original Phase 4 `le=2` lean). At pre-deduct + per-user `pg_try_advisory_xact_lock` cap=1, count=4 stays bounded ($0.16–$0.668/call OpenAI medium→high; $0.16 fal flat); the parallel-fire denial-of-wallet surface T17 proves closed is structurally invariant under count. Personas wanting >4 per turn are explicitly out-of-v0.1; raising further would force a re-think of the per-image credit weight invariant.
- **No edit/inpaint surface** — `ImageBackend.edit()` Protocol method reserved per D-15-X-edit-protocol-reservation but raises `NotImplementedError("edit not supported in v1")`; v1 concrete backends do NOT override. A v1.x editing backend slots in without redesign.
- **6-month lexicon review cadence** — the hard-line filter's closed `MINOR_SET` / `SEX_SET` / `DEVELOPMENTAL_SET` lexicons (T09) require periodic review against NCMEC / IWF / provider safety documentation updates. Documented in [`docs/specs/phase2/spec_15/decisions.md`](docs/specs/phase2/spec_15/decisions.md) D-15-X-hard-line-filter; **first review due 2026-12-06**. Extension of the lexicons goes through code review (closed sets are the structural property — extending is policy, not implementation).
- **BYOK only** — D-15-2 ships BYOK; hosted keys arrive with billing infrastructure post-v0.1. Adding hosted keys is additive (a `BackendConfig.hosted_key_pool` field + a router policy); doesn't break BYOK callers.
- **Suffix-conditioning visual_style template only** — D-15-4 ships the `f"{prompt}, in the style of {style}"` template; provider-specific style parameters not exercised (OpenAI gpt-image-1 + Flux 1.1 [pro] both lack one anyway). Template change is a one-function rewrite.

### Decisions (Spec 15)

**D-15-1** OpenAI gpt-image-1 + Flux 1.1 [pro] via fal.ai at v0.1; **D-15-2** BYOK; **D-15-3** `count <= 4` cap + three size presets (Phase 6 production flip from `le=2`); **D-15-4** suffix-conditioning visual_style template + user-wins conflict-resolution; **D-15-5** authoring suggests `visual_style` optionally (Spec 10 amendment). Micros: **D-15-X-pydantic-boundary-types** (corrects spec §4 `@dataclass` sketches; six-spec precedent); **D-15-X-pre-deduct-credits** (denial-of-wallet structurally impossible only with pre-deduct); **D-15-X-credit-flow-semantics** (reverse-deduct ledger entry pattern (a); no migration); **D-15-X-concurrency-cap** (Postgres `pg_try_advisory_xact_lock`; multi-worker-correct from day one); **D-15-X-workspace-coordination** (reuse Spec 13 storage + GET-route; no new layout, no CSA-2 fork); **D-15-X-hard-line-filter** (categorical refusal ABOVE provider moderation; closed lexicons; content-hash-only audit); **D-15-X-edit-protocol-reservation** (`NotImplementedError("edit not supported in v1")`); **D-15-X-provider-moderation-default** (fal `safety_tolerance=2`); **D-15-X-flagged-image-policy** (any-image-flagged → reject all); **D-15-X-size-rounding** (OpenAI portrait/landscape preset → 1024x1536/1536x1024; capture requested in audit); **D-15-X-audit-event-extension** (four outcome strings via existing `ToolAuditEvent.metadata: dict[str, str]`; no struct change); **D-15-X-observability-shape** (`turn_logs.metadata` with `kind=image_generation`); **D-15-X-license-stack** (fal-client Apache-2.0 + regex Apache-2.0). All in [`docs/specs/phase2/spec_15/decisions.md`](docs/specs/phase2/spec_15/decisions.md), mirrored to [`docs/DECISIONS.md`](docs/DECISIONS.md).

## [persona-web 0.13.0] — 2026-06-06

Spec F2 close-out — **component system + platform shell** (the second half of the frontend trunk of Phase 2). Six rebuilt screens (chat / persona list / persona detail / authoring / run viewer / settings) ride the F2 component kit + the new platform shell + the F1 token system without ever touching the plumbing — `useChat` / `useRun` / `useAuthor` / `serverApi()` / the `src/lib/run.ts` polymorphic normaliser / the `parsePersonaYaml` family all preserved verbatim per the per-route `§*.plumbing` DO-NOT-TOUCH inventory in [`audit.md`](docs/specs/phase2/spec_F2/audit.md). **§7 criteria #1–#13 all met** — #11 (the F1-#7-equivalent design-review judgement) human-signed-off 2026-06-06: *"live smoke is done, everything looks good"* after walking T26 + T27–T31 against the local Postgres + DeepSeek stack. Two user-driven Phase-5 amendments landed mid-implementation: **D-F2-15** (interleaved tool layout in `<MessageElement>`) and a live-markdown / indicator-redesign pass — both surfaced from the same T26 live-smoke session and named explicitly in the decisions log so future readers can reconstruct the user-feedback lineage.

### Added (persona-web — Spec F2)

- **`feat`: component system + platform shell — retokenised UI kit (T03–T12), persona-identity components (T13–T16), measured-locked streaming-text renderer (T17), platform shell + layout primitives (T19–T20), interaction patterns (T21–T23), theme + i18n sweep (T24–T25), six rebuilt screens (T26–T31), component reference + Storybook defer artifact + criterion-#11 evidence package (T32–T34).** Closes Spec F2 Phase 5 (T01–T34) + Phase 6 close-out; **13/13 §7 acceptance criteria met** (#11 human-signed-off 2026-06-06).
- **Retokenised UI primitive kit (T03–T12)** — every shadcn base-nova primitive consumed through F1's `@theme inline`: [`<Button>`](packages/web/src/components/ui/button.tsx) (T03), [`<Card>`](packages/web/src/components/ui/card.tsx) (T04), [`<Badge>`](packages/web/src/components/ui/badge.tsx) (T05), [`<Avatar>`](packages/web/src/components/ui/avatar.tsx) (T06), [`<Sheet>`](packages/web/src/components/ui/sheet.tsx) (T07), [`<DropdownMenu>`](packages/web/src/components/ui/dropdown-menu.tsx) (T08), [`<Tooltip>`](packages/web/src/components/ui/tooltip.tsx) (T09), [`<Input>`](packages/web/src/components/ui/input.tsx) + [`<Textarea>`](packages/web/src/components/ui/textarea.tsx) (T10), [`<Markdown>`](packages/web/src/components/ui/markdown.tsx) (T11). X-F2-5 spike confirmed primitives ~85% token-clean already; the remaining ~15% closed across T03–T12.
- **Persona-identity components (T13–T16)** — [`<PersonaIdentityHeader>`](packages/web/src/components/persona/persona-identity-header.tsx) (T13 — D-F1-5 composite consumer), [`<PersonaCard>`](packages/web/src/components/persona/persona-card.tsx) (T14 — identity-coloured fill via `<PersonaAvatar>` per-persona), [`<MessageElement>`](packages/web/src/components/chat/message-element.tsx) (T15 — interleaved tool layout per D-F2-15), [`<TierBadge>`](packages/web/src/components/chat/tier-badge.tsx) (T16 — closes the 5× `text-[0.65rem]` legacy via `.type-caption`).
- **Streaming-text renderer (T17, D-F2-5 mechanism B)** at [`streaming-text-renderer.tsx`](packages/web/src/components/chat/streaming-text-renderer.tsx) — **measured-locked** against three candidates (A / B / C) on the live local stack at DeepSeek's empirical 33–60 chunks/sec cadence; results recorded at [`measurements/`](docs/specs/phase2/spec_F2/measurements/). Mechanism B (useTransition + rAF-coalesced) ships; C (mutable text-node) remains documented one-component-swap escape hatch with the same `text` prop contract.
- **Platform shell + layout primitives (T19–T20)** — [`<AppShell>`](packages/web/src/components/shell/app-shell.tsx) with desktop sidebar + mobile sheet trigger + theme/persona context wiring; the T20 layout kit ([`<PageBody>`](packages/web/src/components/layout/index.tsx) / `<PageHeader>` / `<Section>` / `<Stack>` / `<Grid>`) consumed by every rebuilt screen so a new page is composed from primitives, not hand-rolled CSS.
- **Interaction patterns (T21–T23)** — T21 loading ([`<SkeletonLine>`](packages/web/src/components/patterns/loading.tsx) / `<SkeletonBlock>` / `<SkeletonAvatar>` / `<Spinner>`), T22 [`<EmptyState>`](packages/web/src/components/patterns/empty-state.tsx) + [`<ErrorState>`](packages/web/src/components/patterns/error-state.tsx) with **D-F2-9 one-template + per-status overrides** (default / 422 / 429 / 402), T23 [`<ToastProvider>`](packages/web/src/components/patterns/toast.tsx) via `sonner@2.0.7` (zero transitive deps, MIT) + [`<FadeTransition>` + `<SlideTransition>`](packages/web/src/components/patterns/transition.tsx).
- **Theme + i18n (T24–T25)** — [`<ThemeToggle>`](packages/web/src/components/theme-toggle.tsx) tri-state (Light/Dark/System) via T08 dropdown-menu with explicit `--motion-duration-fast` on icon swap; F2 primitives stay i18n-agnostic via props convention (consumer passes already-translated strings, primitive renders them).
- **Six rebuilt screens (T26–T31)** under the strangler-fig discipline — every per-route `§*.plumbing` invariant honoured:
  - **T26 chat** — [`(app)/chat/[id]/page.tsx`](packages/web/src/app/(app)/chat/[conversationId]/page.tsx) + [`<MessageElement>`](packages/web/src/components/chat/message-element.tsx) carrying D-F1-5 composite + **D-F2-15 interleaved tool layout** + **live per-chunk Markdown rendering** + redesigned thinking/tool-running indicators (italic label + `py-1.5` breathing + `size-2` dots + 0/200/400ms wave).
  - **T27 persona list** — [`(app)/personas/page.tsx`](packages/web/src/app/(app)/personas/page.tsx) composes `<PageBody>` + `<PageHeader>` + `<Grid cols={{base:1,sm:2,lg:3}}>` + F2 `<PersonaCard>` + `<EmptyState>`; `loading.tsx` Suspense boundary added; scaffold `<PersonaCard>` deleted (the `bg-primary/10` D-F1-5 violation closed).
  - **T28 persona detail** — [`(app)/personas/[id]/page.tsx`](packages/web/src/app/(app)/personas/[id]/page.tsx) composes `<PageBody>` + `<PersonaIdentityHeader size="lg">` + `<Stack>` of `<Section heading><Card>` blocks; `text-[0.65rem]` epistemic badge closed via `.type-caption`.
  - **T29 authoring** — [`(app)/personas/new/page.tsx`](packages/web/src/app/(app)/personas/new/page.tsx) + [`<AuthorWizard>`](packages/web/src/components/personas/author-wizard.tsx) presentation rebuilt: byline `font-mono text-xs tracking-wide uppercase` → `.type-caption`, titles → `.type-display` / `.type-heading`, outer flex → T20 `<Stack>`, shadcn `<Skeleton>` → T21 `<SkeletonLine>`. `useAuthor` hook + 3-round refine cap + `<PersonaEditor>` form ⇄ Monaco sync preserved.
  - **T30 run viewer** — [`(app)/runs/[runId]/page.tsx`](packages/web/src/app/(app)/runs/[runId]/page.tsx) + 5 components (`run-view` / `run-timeline` / `step-card` / `run-status-badge` / `ask-user-prompt`) all retokenised. Every `text-[0.65rem]` → `.type-caption` (5× closed across the run components). `useRun` + `runViewFromEvents` + cancel + respond preserved.
  - **T31 settings** — [`(app)/settings/page.tsx`](packages/web/src/app/(app)/settings/page.tsx) composes `<PageBody>` + `<PageHeader>` + `<Stack>` of cards; credit balance now in `.type-display` (Fraunces hero scale); `balance === 0` surfaces via T22 `<ErrorState status={402}>`; [`<PreferencesCard>`](packages/web/src/components/settings/preferences-card.tsx) retokenised.
- **CI no-literals grep-gate (D-F2-6)** at [`scripts/no-literals.sh`](packages/web/scripts/no-literals.sh) — enforces "no component hard-codes a design value" per criterion #2; Biome `noRestrictedSyntax` fallback because the rule needs file:line-anchored allowlists. Wired into [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
- **5 new Playwright e2e specs** for the rebuilt screens — [`f2-personas-list.spec.ts`](packages/web/e2e/f2-personas-list.spec.ts) + [`f2-persona-detail.spec.ts`](packages/web/e2e/f2-persona-detail.spec.ts) + [`f2-authoring.spec.ts`](packages/web/e2e/f2-authoring.spec.ts) + [`f2-runs.spec.ts`](packages/web/e2e/f2-runs.spec.ts) + [`f2-settings.spec.ts`](packages/web/e2e/f2-settings.spec.ts). Each asserts the F2 data-slot composition + key F2 invariants (identity-coloured avatars, Fraunces type-family, retokenised badge font-size, etc.) without rewriting the existing plumbing-focused `chat.spec.ts` / `runs.spec.ts` / `authoring.spec.ts` / `shell.spec.ts` / `personas.spec.ts`.
- **+94 vitest tests over F1** — 233 total across 23 test files. New: `message-element.test.tsx` (19, includes 9 for D-F2-15), `persona-identity-header.test.tsx` (12), `persona-card.test.tsx` (9), `streaming-text-renderer.test.tsx` (11), `tier-badge.test.tsx` (4), `layout/index.test.tsx` (14), `patterns/loading.test.tsx` (10), `patterns/empty-state.test.tsx` + `error-state.test.tsx` (13), `patterns/toast.test.tsx` + `transition.test.tsx` (9), theme toggle + i18n sweep (4).

### Changed (Spec F2)

- **persona-web bumps `0.12.0 → 0.13.0`** (F1 was 0.12.0; F2 is the next persona-web semver bump).
- **The streaming-text renderer's perception model**: previously rendered raw text until end-of-stream (scaffold pattern avoiding incomplete-syntax flicker). After user feedback during T26 live-smoke, the WHOLE buffer is re-parsed through `<Markdown>` per chunk so block-level structures (headings, lists, code fences) settle at their newline boundary; inline `**bold**` / `` `code` `` settles when both delimiters land. Brief raw-syntax flicker on incomplete inline pairs is the explicit accepted trade-off.
- **The chat message tool layout**: previously stacked all tool cards above text content; D-F2-15 now walks an ordered `message.events[]` log and emits text spans + tool cards inline at stream position. Stacked layout retained as back-compat when `events[]` is absent.
- **The scaffold `<PersonaCard>` (`src/components/personas/persona-card.tsx`, 30 LOC)** deleted at T32 close — orphaned after T27 swap to F2 `<PersonaCard>` (`src/components/persona/persona-card.tsx`, singular path).
- **The scaffold `<MessageBubble>` + its orphan test** deleted at T26 close — replaced by `<MessageElement>` which absorbed the streaming caret + bubble + tool-card composition.

### Notes (Spec F2)

- **Criterion #11 human sign-off, verbatim:** *"live smoke is done, everything looks good"* (2026-06-06). Same agent/human handoff pattern as F1's criterion #7. T26 had an intermediate sign-off (*"it looks okey"*) before the polish iterations (D-F2-15 + live markdown + indicator redesign).
- **Two user-driven Phase-5 amendments** worth flagging — both surfaced from the same 2026-06-06 live-smoke session:
  - **D-F2-15** (interleaved tool layout) — initial T26 stacked layout surfaced four UX issues (no thinking indicator, no tool-running indicator during execution, tool cards clumping at top, text concatenation losing the temporal gap); `<MessageElement>` refactored to walk `events[]` in stream order. Closes D-F2-14's tool-card-placement sub-tension.
  - **Live markdown + indicator redesign** — initial T26 markdown rendered at end-of-stream only; user asked for real-time per-chunk rendering. `<ThinkingIndicator>` + `<ToolRunningIndicator>` redesigned from dots-only (with hidden aria-label) to visible italic label + `py-1.5` breathing + `size-2` dots + 0/200/400ms wave.
- **`lowBalance` / `creditsExhausted` / `creditsExhaustedHint` i18n keys** added to [`en.json`](packages/web/src/i18n/messages/en.json) ahead of a planned `CreditsResponse.low_balance` field on the API side (the generated [`schema.ts`](packages/web/src/lib/api/schema.ts) line 531 currently exposes only `balance`). Pattern matches Spec 13's "v0.2-path-now-feasible enhancement" — small persona-api follow-up flips the inline warning on in [`(app)/settings/page.tsx`](packages/web/src/app/(app)/settings/page.tsx); not v0.2 deferred. Documented in the page's JSDoc + closeout.md.

### Documentation (Spec F2)

- **[`packages/web/COMPONENTS.md`](packages/web/COMPONENTS.md)** — the F2 component reference, ~430 lines, ~30 components across 9 categories (UI primitives / persona-identity / layout / patterns / shell / theme / chat / runs / personas+settings). Per entry: path, server/client tag (D-F2-3), props summary, "use when," "don't use for." **D-F2-2 sibling form chosen** — the X-F2-2 spike's threshold (split if section >150 lines) was crossed at the ~30-component scale. [`DESIGN.md`](packages/web/DESIGN.md) gained a short pointer to `COMPONENTS.md`.
- **[`docs/specs/phase2/spec_F2/storybook-decision.md`](docs/specs/phase2/spec_F2/storybook-decision.md)** — D-F2-4 standalone "did we consider Storybook?" artifact. Three named flip-triggers (capability-UI complexity / library >50 components / non-engineer reviewers), the 3–5-day flip-cost estimate from the X-F2-3 spike, and the v0.1 review-surface map (7 surfaces that do Storybook's work today).
- **[`packages/web/src/app/reference/review-f2/`](packages/web/src/app/reference/review-f2/page.tsx)** — criterion-#11 evidence package. **24 panels** (six rebuilt screens × four panels each: composition + closures + alternate state + dark mode preview). Composes F1 fixture personas (Astrid/Kai/Maren) live through F2 components so the §4 individuality proof is demonstrated within the F2 rebuild surface.
- **[`closeout.md`](docs/specs/phase2/spec_F2/closeout.md)** — the §7 acceptance audit (all 13 criteria walked, #11 human-signed-off 2026-06-06), check matrix, decisions log, "What Spec F2 hands to future specs" section (F3 capability UIs + the `lowBalance` v0.2-path-now-feasible enhancement + closure traces + the four D-F2-14 redirect closures).
- **[`packages/web/scripts/no-literals.sh`](packages/web/scripts/no-literals.sh)** — D-F2-6 grep-gate script with documented allowlist (3 documented exceptions + 6 LEGACY entries each tied to an audit.md rationale).

### Decisions (Spec F2)

**D-F2-1** (shadcn retokenise vs custom — **retokenise**; custom only for the 5 persona-specific components); **D-F2-2** (component reference doc form — **sibling COMPONENTS.md** chosen after the >150-line threshold triggered); **D-F2-3** (server vs client per-component — documented in COMPONENTS.md, ~21 server / ~14 client across the F2 surface); **D-F2-4** (Storybook — **defer for v0.1** with three named flip-triggers per [`storybook-decision.md`](docs/specs/phase2/spec_F2/storybook-decision.md)); **D-F2-5** (streaming-renderer mechanism — **mechanism B**, useTransition + rAF-coalesced; **measured-locked 2026-06-05** at the in-tree harness against synthesised DeepSeek-cadence replay; C documented escape hatch); **D-F2-6** (CI grep-gate — Biome `noRestrictedSyntax` fallback via [`scripts/no-literals.sh`](packages/web/scripts/no-literals.sh)); **D-F2-7** (`<MessageElement>` avatar — once-per-turn rule); **D-F2-8** (`<PersonaIdentityHeader>` `showConstraints` opt-in); **D-F2-9** (one-template `<ErrorState>` + per-status overrides for default / 422 / 429 / 402); **D-F2-10** (`sonner@2.0.7` for toasts, zero-dep + MIT); **D-F2-11** (mobile breakpoints — Tailwind defaults `sm 640 / md 768 / lg 1024 / xl 1280 / 2xl 1536`); **D-F2-12** (streaming caret colour — **vermilion `--primary`**, final-locked at T34 criterion-#11 review per D-F2-14(d) shared lock); **D-F2-13** (pre-first-token thinking state — reuse F1 `/reference/run` indicator pattern); **D-F2-14** (F1 carry-forward redirect dispositions — all four closed at T34 review); **D-F2-15** (interleaved tool layout in `<MessageElement>` — post-T22 user-driven amendment 2026-06-06 from live-smoke session). All in [`docs/specs/phase2/spec_F2/decisions.md`](docs/specs/phase2/spec_F2/decisions.md), mirrored to [`docs/DECISIONS.md`](docs/DECISIONS.md).

## [0.13.0] — 2026-06-06

Spec 13 close-out — **vision (image input)**. Threads multimodal `ConversationMessage` through four layers (core schema → backends → runtime router → API uploads) without disturbing the text-only invariant. **§9 criteria #1–#14: 13/14 ✅ MET in-CI + 1/14 🟦 SERIALISATION-MET (in-CI) / LIVE deferred to manual operator pass** (criterion #4 live half, per Phase 2 fold-in #9 agent/human discipline). T20 closed the headline e2e gap by widening `PostMessageRequest.images` so multi-image messages now travel end-to-end through `web → POST /messages → chat_service → loop → messages.images JSONB`; the load-bearing proof is `test_multi_image_message_preserves_order`.

### Added (Spec 13 — Vision / image input)

- **`feat(core)`: vision (image input) — multimodal `ConversationMessage`, per-provider serialisation, vision capability matrix, router pre-filter, image upload + serve endpoints.** Closes Spec 13 Phase 5 (T01–T19) + T20 gap-closer + R-2 reconciliation. (Spec 13)
- **`MessageContent` discriminated union** — `TextContent | ImageContent` (Pydantic v2 `Annotated[..., Field(discriminator="type")]`) at [`packages/core/src/persona/schema/content.py`](packages/core/src/persona/schema/content.py). `ConversationMessage.content` widened from `str` to `str | list[MessageContent]` **additively** (D-13-X-now option (c)); the `_reject_single_text_as_list` validator preserves the byte-for-byte text-only invariant (criterion #1). Phase 1 regression corpus (20 `ConversationMessage(...)` snapshots from T01 source audit) reconstructs identically. (T02, T03)
- **`PostMessageRequest.images` field (cap 4)** closing the e2e gap — `Field(min_length=1, max_length=4)` widening at the API request boundary; closes the headline gap so multi-image messages now travel end-to-end through `web → POST /messages → chat_service → loop → messages.images JSONB`. **LF-13-2 lesson:** built-in `Field(...)` constraints over `@field_validator` for cap-style API fields (see [`docs/DECISIONS.md`](docs/DECISIONS.md) LF-13-2 [project-wide]). (T20)
- **Vision capability matrix + `supports_vision`** — `_VISION_CAPABILITY` in [`packages/core/src/persona/backends/openai_compat.py`](packages/core/src/persona/backends/openai_compat.py) lists, **verified-as-of-cutoff (D-13-3)**, `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` (OpenAI) + the configured Claude Sonnet 4.x family (Anthropic). Two new domain exceptions in `errors.py`: `BackendVisionNotSupportedError` + `NoVisionTierConfiguredError`, both **flat under `PersonaError`** per D-13-X-error-hierarchy (two subclasses, D-03-1 precedent). (T04)
- **Per-provider image serialisation** — `_message_to_anthropic` emits Anthropic image content blocks (base64-inline per D-13-2); `_message_to_openai` emits multi-part `image_url` data-URLs (D-13-2 simplicity branch). Both preserve content-list order so multi-image messages serialise in order (criterion #11). (T05, T06)
- **Ollama + HF local fail-loud** — `OllamaBackend` and `HFLocalBackend` raise `BackendVisionNotSupportedError` via `_guard_vision` on image-bearing turns rather than silently dropping. Ollama happy-path threads images through the `images` field when a vision-capable Ollama model is wired. Reconciled by Wave-R2 after the harness-revert event documented in [`state.md`](docs/specs/phase2/spec_13/state.md). (T07)
- **`PromptBuilder` multimodal placement** — `prompt.py` widening preserves the §5.1 system block ordering; image-bearing user turns place image content inline with text per the content-list iterate-in-order rule (the explicit interleave rule, fold-in tighten item). (T08)
- **Router pre-filter + turn-log visibility** — `_candidate_tiers` + `turn_has_image` in `router.py` raise `NoVisionTierConfiguredError` when no candidate tier is vision-capable (criterion #7) and force escalation to a vision-capable tier even when other heuristics would not (criterion #6). `tier_used` flows through `loop.py` to the existing spec-05 `TurnLogWriter` so vision tier cost is visible per criterion #12. (T09)
- **`image_service` validation + Pillow downscale** — [`packages/api/src/persona_api/services/image_service.py`](packages/api/src/persona_api/services/image_service.py): 4-format pre-decode bomb-prevention guard (PNG IHDR + JPEG SOF + WebP RIFF + GIF LSD) at `_pre_decode_dims`; Pillow downscale + EXIF strip at `_maybe_downscale`; `Image.MAX_IMAGE_PIXELS = 50_000_000` ceiling. Pillow declared per ENGINEERING_STANDARDS §3 (D-13-X-pillow; HPND license; persona-api-only). Bomb fixtures committed at [`packages/api/tests/fixtures/decompression_bomb.{png,jpeg,webp,gif}`](packages/api/tests/fixtures/). (T10a, T10b)
- **`POST /v1/personas/:id/uploads` + `GET /v1/personas/:id/uploads/:ref`** — [`packages/api/src/persona_api/routes/uploads.py`](packages/api/src/persona_api/routes/uploads.py) routes registered in [`app.py`](packages/api/src/persona_api/app.py) with `workspace_root` on `app.state` (config in [`config.py`](packages/api/src/persona_api/config.py)). Both endpoints structurally RLS-scoped (D-08-1) — cross-tenant access is impossible (criterion #14). `test_rls_per_endpoint.py` extended in-place to cover both endpoints (T14 folded in). Workspace storage path `{workspace_root}/{owner_id}/{persona_id}/uploads/{ref}` per D-13-4. (T11)
- **Alembic migration `004_add_message_images`** — adds the nullable `messages.images JSONB` column per D-13-X-now option (c); idempotent one-line `ALTER TABLE ... ADD COLUMN IF NOT EXISTS images JSONB` mirroring spec-08's shipped `002_add_message_channel` template.
- **Workspace cascade-delete** — `services/persona_service.py` + `routes/personas.py` delete the persona's workspace subtree on persona deletion; verified by [`packages/api/tests/integration/test_workspace_cascade.py`](packages/api/tests/integration/test_workspace_cascade.py). D-13-4 cascade discipline; under the 200 LOC cap; no escalation needed. (T12)
- **Store-by-reference regression** — [`packages/api/tests/integration/test_messages_bounded_by_references.py`](packages/api/tests/integration/test_messages_bounded_by_references.py): 3 regression tests proving (a) `messages` row total stays bounded across 10 image turns, (b) `_message_to_anthropic` gated emits no large intermediate, (c) `_message_to_openai` gated emits no large intermediate — image bytes live once in workspace; the message store never bloats with per-turn base64 (criterion #10, D-13-X-now option (c)). (T13)
- **Multi-image ordering regression (criterion #11 load-bearing proof)** — [`packages/api/tests/integration/test_conversations.py::test_multi_image_message_preserves_order`](packages/api/tests/integration/test_conversations.py) drives a 4-image POST end-to-end through `PostMessageRequest → conversations.py → chat_service → loop → messages.images JSONB` and asserts list ordering is preserved across the persistence boundary. (T20)
- **Default-suite mocked vision round-trip** — [`packages/core/tests/unit/backends/test_vision_round_trip.py`](packages/core/tests/unit/backends/test_vision_round_trip.py): 2 mocked round-trip tests (Anthropic + OpenAI) so criterion #4's *serialisation* coverage runs on every PR without paid API keys. (T15, NEW per Phase 2 fold-in #7)
- **External vision smokes (scaffold-ready)** — [`packages/api/tests/external/test_vision_smoke.py`](packages/api/tests/external/test_vision_smoke.py) (T16; parametrized [anthropic-claude-sonnet-4-6, openai-gpt-4o]) + [`packages/api/tests/external/test_vision_streaming_anthropic.py`](packages/api/tests/external/test_vision_streaming_anthropic.py) (T17). Both `@pytest.mark.external`; fixture at [`tests/fixtures/vision_test_image.png`](packages/api/tests/fixtures/vision_test_image.png). Live runs captured in [`docs/specs/phase2/spec_13/state.md`](docs/specs/phase2/spec_13/state.md) "Manual smoke results" at Phase 6 close-out (fold-in #9). (T16, T17)

### Changed (Spec 13)

- **`BackendVisionNotSupportedError` + `NoVisionTierConfiguredError`** flat under `PersonaError` (D-13-X-error-hierarchy; two subclasses per D-03-1 precedent — introduce a parent only when a third lands).
- **`ConversationMessage.content` widens to `str | list[MessageContent]`** additively (D-13-X-now option (c)). Existing text-only constructors are byte-for-byte unchanged.

### Notes (Spec 13)

- **Criterion #4 LIVE half deferred to manual operator pass per Phase 2 fold-in #9.** Scaffolds at [`packages/api/tests/external/`](packages/api/tests/external/) + fixture at [`packages/api/tests/fixtures/vision_test_image.png`](packages/api/tests/fixtures/vision_test_image.png) on disk; runs when `ANTHROPIC_API_KEY` + `OPENAI_API_KEY` are set in the operator shell. No code change required.
- **Pillow added as a `packages/api` dependency**; license-stack discipline per D-13-X-pillow (HPND vs Apache-2.0 — `persona-core` stays Apache-2.0-only; Pillow lives in `persona-api` where the upload service runs).
- **LF-13-2 [project-wide]** — Pydantic `field_validator` → `Field(min_length, max_length)` for cap-style API fields. Recorded in [`docs/DECISIONS.md`](docs/DECISIONS.md) and [`docs/specs/phase2/spec_13/research.md`](docs/specs/phase2/spec_13/research.md) §"Implementation findings".

### Documentation (Spec 13)

- **§8 PDF-boundary** — [`docs/specs/phase2/spec_13/pdf_boundary.md`](docs/specs/phase2/spec_13/pdf_boundary.md) + `spec_13_vision.md` §8 cross-ref document the text-extractable-PDF → Spec 14 vs image-only/scanned-PDF → Spec 13 vision contract. The rasterise-scanned-PDF handoff lives in Spec 14 and consumes this spec's vision capability. (T18)
- **Verify-at-deploy (D-13-3)** — T19 re-fetched `https://platform.openai.com/docs/guides/vision` + `…/docs/models` (both 301-redirect to `developers.openai.com`). Both fetches returned a hallucinated lineup centred on `gpt-5.5`/`gpt-5.4`/`gpt-5.4-mini` (none of which exist in the public OpenAI lineup as of the knowledge cutoff). **The result was rejected** per the verified-as-of-cutoff discipline; the committed matrix (`gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`) stands unchanged. Rejection documented in [`state.md`](docs/specs/phase2/spec_13/state.md) "Verify-at-deploy — OpenAI vision matrix" block — this rejection IS the discipline working. Phase 6 close-out candidate: switch the verify-at-deploy path to `openai-python` SDK's `models.list()` / OpenAI Cookbook model-card READMEs (see [`docs/specs/phase2/spec_13/closeout.md`](docs/specs/phase2/spec_13/closeout.md) and [`research.md`](docs/specs/phase2/spec_13/research.md) §"Implementation findings"). (T19)
- **Close-out audit** — [`docs/specs/phase2/spec_13/closeout.md`](docs/specs/phase2/spec_13/closeout.md) records the §9 audit walk + Definition of Done checklist + Phase 6 candidate dispositions + what Spec 13 hands to future specs.

### Decisions (Spec 13)

D-13-X-now (option (c): image refs travel via the new `messages.images` JSONB column — T13 tests confirm no base64 leaks into other columns); D-13-X-error-hierarchy (two subclasses flat under `PersonaError`); D-13-1 (downscale-with-hard-ceiling per Pillow); D-13-2 (Anthropic base64-inline, OpenAI data-URL); D-13-3 (verified-as-of-cutoff matrix for OpenAI gpt-4o family; revisit per the Spec 02 pattern); D-13-4 (workspace storage at `{workspace_root}/{owner_id}/{persona_id}/uploads/{ref}` + cascade-delete on persona deletion); D-13-5 (per-message + per-payload caps); D-13-X-matrix-extract-rule (locked); D-13-X-rate-limit-bucket; D-13-X-pdf-contract; D-13-X-pillow (Pillow declared per §3); LF-13-1 (Spec 11 carry-forward); **LF-13-2 [project-wide]** (Pydantic `Field(min_length, max_length)` for cap-style API fields — see [`docs/DECISIONS.md`](docs/DECISIONS.md)). All in [`docs/specs/phase2/spec_13/decisions.md`](docs/specs/phase2/spec_13/decisions.md), mirrored to [`docs/DECISIONS.md`](docs/DECISIONS.md).

## [1.0.0] — 2026-05-29

Spec 11 close-out — **the launch spec, and the project's v0.1.0 release.**

**Version mapping (D-11-8).** This is the first **public, API-stable** release of the open-source `persona-core` library — semver-1.0. The per-spec `[0.x.0]` line ends here; `[0.10.0]` → `[1.0.0]`. The project / repos carry the **`v0.1.0` git tag** (the architecture / §10 milestone name for the September release of the *system*). Library 1.0 = stable public API under Apache 2.0; product v0.1 = first demoable system. Not in conflict; both are intentional.

Spec 11 is cross-cutting — it builds no new layer. It proves the whole system works end-to-end at scale, authors the demo content, finishes every stub, hardens the public surface, and **prepares** (not executes) the irreversible launch actions per the agent/human line (D-11-11). The full-100 conversation soak on Astrid measured `episodic=100, compacted_up_to=193 (≈19 compactions), max_prompt_tokens=20553`, zero 500s, identity present at turn 100, early episodic retrievable by content — the architecture's thesis (typed memory + tier routing yields a coherent persona at scale) is empirically validated. The 15-step agentic run completes cleanly; the 5-way concurrent run confirms RLS holds under load.

Along the way the soak surfaced — and we fixed — **five interlocking latent bugs in the OpenAI/DeepSeek native tool-calling protocol** that no prior spec exercised live, plus **a sixth** found by the launch security review (an httpx redirect bypass on the SSRF guard). All six fixes are CI-gated; the soak is the live integration proof.

### Added (persona-core — Spec 11)
- **Three example personas** ([`packages/core/examples/`](packages/core/examples/)) — Astrid (Norwegian tenancy law), Kai (research assistant), Maren (writing coach). Astrid validates clean from spec §3.1 as written; Kai + Maren hand-authored at full §3.2/§3.3 depth. CI-gated by [`test_examples_validate.py`](packages/core/tests/unit/test_examples_validate.py) — schema validates, ≥3 constraints, ≥1 safety constraint, ≥1 non-`fact` worldview, declared skills' tools are covered. (T01, D-11-10)
- **SSRF guard in `web_fetch`** — resolved-IP block of private / loopback / link-local (incl. the 169.254.169.254 metadata endpoint) / reserved / multicast / unspecified, DNS-rebind safe (`socket.getaddrinfo` on the *resolved* IP). **Plus** the T07b security-review fix: replaced httpx's transparent `follow_redirects=True` with a manual hop loop (max 5) that **re-runs the SSRF check on each `Location` header** — closes a public→private redirect bypass. 10 adversarial unit tests. (T07 / T07b, D-11-6)
- **Structured `tool_calls` on `ConversationMessage`** — additive frozen field (`list[ToolCall]`, default empty) so the runtime loops can attach the assistant's native tool_calls to the message; `_message_to_openai` + `_message_to_anthropic` now serialize the native `tool_calls` / `tool_use` shape, matching the OpenAI/DeepSeek + Anthropic protocols. (T03 finding #2)
- **Streaming `call_id` reconstruction in `OpenAICompatibleBackend.chat_stream`** — `id_by_index` resolves the stable tool-call id by `tc.index` (DeepSeek sends `id` only on the FIRST delta; continuations are `None`); synthesises `call_{idx}` when the provider omits the id entirely. (T03 finding #3)
- **Metadata-key fix in the openai/anthropic serializers** — `_message_to_openai` and `_message_to_anthropic` now read `metadata.get("tool_call_id")` (matching what `format_tool_result` writes) instead of `metadata.get("call_id")`. Latent forever; first hit live in T03. (T03 finding #4)

### Added (persona-runtime — Spec 11)
- **`ConversationLoop._dispatch`** — converts `ToolNotAllowedError` / `ToolExecutionError` into a `ToolResult(is_error=True, …)` fed back to the model, so a hallucinated / empty tool name **does not crash the SSE** ("response already started"). Mirrors the existing agentic-loop `_dispatch`. Regression test: bad-then-good recovery. (T03 finding #1)
- **Assistant-with-tool_calls message on the native path** — both `ConversationLoop.turn` and `AgenticLoop._handle_tool_calls` now append an assistant `ConversationMessage` carrying `tool_calls=round_calls` (chat loop) / `tool_calls=list(response.tool_calls)` (agentic loop) BEFORE the tool-result messages, when `backend.supports_native_tools`. Shim providers carry calls as text, unchanged. (T03 finding #2)
- **`StepHistoryCompactor._recent_start`** — walks the verbatim-tail boundary back over leading `tool` messages so the kept slice never begins with a dangling tool result whose issuing assistant got summarised away. Closes the agentic compactor's 400 ("'tool' must follow a message with 'tool_calls'"). Updated unit test asserts the tail never starts on a `tool` role. (T03 finding #5)

### Added (persona-api — Spec 11)
- **Credits zero-guard (the §5 finish; D-11-12)** — `credits_service.require_credits` raises `CreditsExhaustedError` → **HTTP 402** *before* a stream / run starts, called at the top of chat (`POST /v1/conversations/:id/messages`), runs (`POST /v1/personas/:id/runs`), authoring (`POST /v1/personas/author`), and refinement (`POST /v1/personas/author/refine`). The post-success `deduct` (D-08-6) is unchanged. `CreditsResponse.low_balance: bool` field (under-10 000 threshold) populated by `/v1/me/credits` so the web app surfaces the warning inline. (T04)
- **Two committed Grafana dashboards** ([`packages/api/dashboards/`](packages/api/dashboards/)) — **§6.1 per-persona usage** (conversations, avg turns, episodic chunk count, compaction events) and **§6.2 routing health** (tier distribution stacked-area from `turn_logs.tier_used`, cost per conversation, tool calls per turn, skill activations per day). Both verified rendering against real soak data. The setup README provides the **`grafana_ro BYPASSRLS` SQL** — a plain `SELECT`-only role gets RLS-filtered to zero rows (D-11-5 sub-finding). §6.3 system-health **documented as post-September** (no source telemetry today). (T05)
- **Soak-test harness** ([`packages/api/tests/soak/test_soak.py`](packages/api/tests/soak/test_soak.py)) — three runners (100-turn conversation, 15-step agentic, 5-way concurrent), `@pytest.mark.external`. In-process FastAPI `TestClient` + fake `verify_token` + the **real `RuntimeFactory`** (real DeepSeek backend + real `SentenceTransformerEmbedder`); a `PromptBuilder.build` spy enables the identity-at-turn-N assertion. (T02, D-11-13)

### Added (project — launch prep)
- **`LAUNCH_CHECKLIST.md`** at the repo root — every spec §10 item marked **agent-done** (1, 2, 3, 4, 6, 7-write) or **prepared-human-executes** (5-deploy, 8 record, 9 public, 10 deploy, 11 deploy, 12 tag). The Pre-flight section mirrors the new CI matrix + the four manual pre-tag items (Playwright e2e, authoring corpus eval, soak suite, `/chat` Lighthouse). The honest-headline close-out wording is in §9. (T10, D-11-11)
- **Deploy artifacts** — [`packages/api/Dockerfile`](packages/api/Dockerfile) (single uvicorn worker per S08-4; multi-stage build via `uv sync --frozen --all-packages --no-dev`), [`deploy/docker-compose.production.yml`](deploy/docker-compose.production.yml) (API + Postgres+pgvector co-located; migrations explicit, not auto-on-startup), [`deploy/.env.production.example`](deploy/.env.production.example) — the full env-var manifest including **`PERSONA_API_JWT_AUDIENCE`** (T07 deploy-config fix for the open spec-08 MEDIUM, D-08-4). (T10)
- **`packages/web/vercel.json`** — framework=nextjs, regions=fra1, security headers (`X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`). (T10)
- **Screencast shot-list** — [`docs/specs/spec_11/screencast_shotlist.md`](docs/specs/spec_11/screencast_shotlist.md) — the eight architecture-§2 steps with the exact clicks + lines + expected results, DeepSeek primary + Sonnet backup one-env-change procedure, the pre-recording checklist (credits headroom, web-search key, browser config). **Human records.** (T09, D-11-9)
- **`persona-core` README** — [`packages/core/README.md`](packages/core/README.md): 143 lines (≤200, acceptance #6), the five §8 questions answered (what / install / use / hosted / contribute), plus an explicit **Known Limitations** section so a v0.1 reader sees what's deferred. (T08)

### Changed (project — CI)
- **`.github/workflows/ci.yml`** — closes the launch-CI coverage gap (T07b follow-up, commit `2774761`):
  - New `web` job: `pnpm install --frozen-lockfile` + `typecheck` + `lint` (Biome) + `build` (Next 16 + Turbopack, CI placeholder env) + Vitest (35 tests). Node 20 + pnpm 9.
  - `lint-and-type-check` extended to `mypy --strict` on **core AND runtime** + standard `mypy` on `api` — matching the local matrix exactly.
  - The `exit 5 = no tests collected` bootstrap shim is **removed** from both pytest jobs (we have 1091 tests; no-tests is now a real failure).
  - Integration DSN switched from `postgresql+asyncpg://` → `postgresql+psycopg://` per D-07-1; the `persona_app` non-superuser role is provisioned in the workflow so the RLS suite runs instead of skipping.
  - **NOT in CI** (documented in `LAUNCH_CHECKLIST.md` #9): Playwright e2e (needs live API + Clerk + DeepSeek + Docker); authoring corpus eval (paid, manual); soak suite (paid, manual); Lighthouse on `/chat` (manual).

### Fixed (security)
- **SSRF redirect bypass in `web_fetch`** — surfaced by the launch security-reviewer. Prior code used `httpx.AsyncClient.get(url, follow_redirects=True)` which would chase a public server's 302 to a private IP. Replaced with a manual hop loop that re-checks SSRF on each `Location`. Verified by `test_blocks_redirect_to_private_ip` (the mock transport raises if asked to fetch the private redirect target). (T07b)

### Decisions resolved (D-11-1 .. D-11-14)
All in [`docs/specs/spec_11/decisions.md`](docs/specs/spec_11/decisions.md), mirrored to [`docs/DECISIONS.md`](docs/DECISIONS.md). Headlines: **D-11-11** (the agent/human line as structural discipline); **D-11-4** (eviction measure-first → document post-September with age-based key; soak measured `episodic=100` after 100 turns — bounded); **D-11-5** (ship §6.1/§6.2, document §6.3 + the `grafana_ro BYPASSRLS` sub-finding); **D-11-6** (SSRF resolved-IP block — fix, not document); **D-11-7** (deferred-debt dispositions: JWT-aud = deploy-config; TOCTOU / JWKS / multi-process JSONL / single-worker = documented Known Limitations); **D-11-8** (the version mapping above); **D-11-9** (DeepSeek primary + Sonnet backup for the screencast); **D-11-12** (credits pre-flight 402 + `low_balance` field); **D-11-13** (soak harness in-process + real `RuntimeFactory`); **D-11-14** (canonical error shape unchanged in v0.1 — `{"error","detail"}`, future migration documented).

---

## [0.10.0] — 2026-05-29

Spec 10 close-out. **LLM-assisted persona authoring** — the prompt-engineering spec ("looks easy and isn't"). A natural-language description becomes a complete, valid v1.0 persona YAML **plus 2–4 clarifying questions**, with a refinement loop, reviewed before save. Unlike every prior spec, "done" here is an **empirically measured compliance rate**, and per the Phase-1 raise it is **model-agnostic**: the contract must hold on the *weakest* supported model. It does — at prompt **v2**, the committed 24-description corpus scores **24/24 on every metric (valid first-attempt, after-retry, safety constraint, adversarial pass/fail, epistemic diversity, sections-complete) on BOTH `deepseek-chat` (floor) and `claude-sonnet-4-6` (frontier)**. The prompt raises compliance probability; the validate-retry-with-error-feedback loop guarantees the contract regardless of model.

### Added (persona-api — Spec 10)
- **The versioned authoring prompt** ([`services/authoring_prompt.py`](packages/api/src/persona_api/services/authoring_prompt.py)) — `AUTHORING_PROMPT_VERSION` + the system prompt with the full v1.0 schema, the §3.1 instructions, **2 few-shot example personas** (the cross-model compliance lever, S10-2; unit-validated so they can't drift), call-time tool/skill injection (S10-3), and the `---QUESTIONS---` output contract. `build_authoring_prompt` / `build_refinement_prompt`. (T01, D-10-4)
- **Lenient response parsing** ([`services/authoring_parse.py`](packages/api/src/persona_api/services/authoring_parse.py)) — `split_response` tries `---QUESTIONS---`, then `Questions:`/`Clarifying questions:` (only with a JSON array), then whole-as-YAML; malformed questions degrade to none. Never raises. (T02)
- **The draft generator + retry loop** ([`services/authoring_service.py`](packages/api/src/persona_api/services/authoring_service.py)) — `generate_authoring_draft` / `refine_authoring_draft`: chat → parse → validate → **retry once with the validation errors fed back** → `AuthoringDraft`; best-effort YAML + errors on retry-exhaustion (never raises). The retry is the model-agnosticism mechanism. (T03, D-10-3)
- **The `/author` contract change + the refine endpoint** — `POST /v1/personas/author` now returns an **`AuthoringDraft` `{yaml, questions, prompt_version, errors?}`** and creates **no** persona row (creation stays on `POST /v1/personas`); new **`POST /v1/personas/author/refine`** (`{current_yaml, question, answer, round}`) with a server-backstopped **3-round cap** (`RefinementLimitError` → 422). Author + refine deduct the flat authoring credit; creation is free; author/refine audit with an empty target. (T04/T05, D-10-2, D-10-5, D-10-6, D-10-8)
- **The committed test corpus + per-model eval harness** — `packages/api/tests/fixtures/authoring_corpus.yaml` (24 descriptions: every category + spec-11's 3 demo seeds) + deterministic metric functions (CI-tested) + an `@pytest.mark.external` runner that scores the per-(model × prompt-version) matrix. (T06/T08, D-10-1, D-10-7; matrix in [`docs/specs/spec_10/eval_results.md`](docs/specs/spec_10/eval_results.md))

### Added (persona-web — Spec 10)
- **The authoring draft → refine → save UI seam** — `useAuthor` now returns a draft (`author`) + a `refine` method; the wizard reviews an **unsaved draft** and saves via a new `createPersona` action (the draft-before-save flow, replacing spec-09's create-immediately); `persona-editor.tsx` fills the `:109` seam with a **clarifying-questions section** — answer one → re-generate → re-mount with the new draft, hidden after 3 rounds (D-09-11 → D-10-2/D-10-5). Regenerated REST client (`pnpm gen:api`). Browser-verified (authoring.spec: draft → questions → refine → save creates). (T07)

### Fixed (persona-core — Spec 02 bug, surfaced by Spec 10)
- **Anthropic backend base_url** — `DEFAULT_BASE_URLS["anthropic"]` carried a trailing `/v1/` that the `anthropic` SDK double-appended → every Anthropic call hit `…/v1/v1/messages` → 404. Never caught live because spec-09 forced all tiers to DeepSeek. Dropped the suffix; added a regression unit test + an `@pytest.mark.external` real-call smoke test. This unblocked the frontier half of the model-agnostic matrix. (T00, D-10-9)

### Decisions
- **D-10-1** model-agnostic compliance bar (floor `deepseek-chat` + frontier `claude-sonnet-4-6`; tune to the floor; record the matrix) · **D-10-2** the `/author` draft-return + refine fork · **D-10-3** empirical/deterministic test split · **D-10-4** prompt home + 2 few-shots + version threading · **D-10-5** stateless 3-round cap · **D-10-6** `AuthoringDraft` shape (codegens) · **D-10-7** corpus + results location · **D-10-8** credits + targetless audit · **D-10-9** Anthropic base_url fix.

## [0.9.0] — 2026-05-29

Spec 09 close-out. The **web app** (`packages/web`, `persona-web`) — the product surface and the first non-Python package. Next.js 16 (App Router, Turbopack) + React 19 + Tailwind v4 + shadcn (base-nova) + Biome + next-intl, with Clerk auth. The whole spec is "present the API's data well and stream its responses smoothly," so the headline is the **two-surface API contract (D-09-1)**: the REST surface is a committed, generated `openapi-typescript` + `openapi-fetch` client; the two SSE streams are **hand-mirrored** because OpenAPI can't model them — and they use *different envelopes* (chat = bare payload, run = the whole `RunEvent`). Demoable end-to-end: sign up → author a persona from one sentence → chat streaming a real model with a tier badge → watch an agentic run unfold. Every user-facing slice is verified in a real browser (full Playwright suite green). This spec also closed three Spec-08 gaps it surfaced (below).

### Added (persona-web — Spec 09)
- **Scaffold + contract plumbing** — Next 16/Tailwind v4/shadcn/Biome/next-intl scaffold; committed generated REST client (`src/lib/api/`, regen via `pnpm gen:api`) with a Clerk-Bearer middleware + structured `ApiError`/rate-limit surfacing; hand-mirrored SSE types (`src/lib/sse-types.ts`, both envelopes) + a `fetch`+`ReadableStream` `consumeSSE` (not `EventSource` — it can't send a Bearer on a POST). (D-09-1)
- **Clerk auth** — `<ClerkProvider>`, `src/proxy.ts` (Next-16 middleware) protecting the `(app)` group, sign-in/up routes, server/client token helpers; a Clerk JWT template's `aud` aligns with `PERSONA_API_JWT_AUDIENCE` (RS256 + static PEM). An automated Clerk E2E harness (`+clerk_test` signup → saved storageState). (D-09-2)
- **App shell + persona pages** — "editorial instrument" design (warm paper/ink + vermilion, Fraunces display, tier tokens cool→hot); responsive sidebar/sheet; persona list + detail (server components via `serverApi`, no CORS); start-chat / start-run entries.
- **Chat (KEYSTONE)** — streaming chat over SSE with a visible identity header, character-by-character text, collapsible tool-call cards, and a per-turn tier badge; reconnect = re-fetch history (never resume raw SSE). (T06)
- **Run viewer** — the agentic-run timeline over SSE: `useRun` catches up from `GET /runs/:id`, attaches the live `/events` stream, and reconciles-not-resumes on drop; `src/lib/run.ts` normalises **both** `runs.steps` shapes (RunEvent event-log while running / `Step` dicts when terminal) into one timeline; step cards (thinking / tool calls / reasoning / inline ask-user → `/respond` / Markdown final), status badges, cancel. Final rendered with `react-markdown` (D-09-13, `/runs` route only — off the chat bundle). (T07)
- **Authoring (MARQUEE)** — NL description → live frontier author (creates immediately, D-09-11) → structured `PersonaForm` (identity / self-facts / worldview with epistemic + confidence / constraints / tools + skills) ⇄ lazy Monaco `YAMLEditor` (`next/dynamic {ssr:false}`, off the chat bundle, D-09-8); form↔YAML sync with the parsed object as the single source of truth (invalid YAML keeps the last valid form + blocks save, D-09-9); a designed 10–30s loading state; `new` + `[id]/edit` pages; a clean `useAuthor` seam + placeholder slot for Spec-10's draft/questions/refinement. (T08)
- **Settings + conversations** — credit balance + per-turn usage table; theme, tier-badge-visibility, and language (pseudo-locale) toggles persisted to localStorage (D-09-5); a real conversations list. (T09)
- **Landing page** — public `/`: editorial hero, four feature beats, tier-escalation motif, auth-aware CTAs (server `auth()`). (T10)
- **i18n + polish** — every user-facing string through `next-intl` `t()` (English shipped); a generated pseudo-locale (`xx`, cookie-selected) proves full coverage (#9); responsive at 375px with no horizontal scroll across all routes (#5); dark mode throughout; Monaco proven absent from the chat bundle (#10). (T11)

### Added (API — Spec 09)
- **Auto-title on first message** — the first turn of a conversation generates a short title from the user's message via the small tier (best-effort: a summariser failure keeps the default title and never breaks the turn). Wired as an injectable `title_builder` on `app.state` (the runtime factory builds it from the small tier). ([`chat_service.py`](packages/api/src/persona_api/services/chat_service.py), [`runtime_factory.py`](packages/api/src/persona_api/services/runtime_factory.py))
- **`personas.avatar_url`** — a nullable presentation field (not part of the persona YAML schema) for the persona-list / chat-header visual identity. Accepted on create/PATCH (PATCH leaves it untouched when omitted), surfaced on `PersonaSummary`/`PersonaDetail`. Migration `003_add_persona_avatar`. ([`003_add_persona_avatar.py`](packages/api/alembic/versions/003_add_persona_avatar.py))
- **`DELETE /v1/conversations/:id`** — deletes a conversation and cascades to its messages + turn_logs (FK `ON DELETE CASCADE`); RLS-scoped (404 cross-tenant). ([`conversations.py`](packages/api/src/persona_api/routes/conversations.py))

### Fixed (gaps Spec 09 surfaced)
- **CORS** — `CORSMiddleware` in `app.py::create_app` (origins from `PERSONA_API_CORS_ORIGINS`, default `http://localhost:3000`; `allow_credentials=False` since auth is Bearer; exposes the `X-RateLimit-*` + `Retry-After` headers). New `cors_origins` field in `config.py`. The browser client needs it for any non-server-component call. ([`app.py`](packages/api/src/persona_api/app.py), [`config.py`](packages/api/src/persona_api/config.py))
- **JIT user provisioning** — `services/user_service.py::ensure_user` (idempotent `INSERT … ON CONFLICT (id) DO NOTHING` via a superuser `admin_engine`), called from `auth/deps.py::get_current_user`. A fresh Clerk user has no `users` row (Spec 08 deferred webhook mirroring) yet everything FKs `users.id`; this JIT upsert is the v0.1 equivalent of the prod provider-webhook path. ([`user_service.py`](packages/api/src/persona_api/services/user_service.py), [`deps.py`](packages/api/src/persona_api/auth/deps.py)) Re-verified: ruff + mypy clean, all 59 integration tests pass.
- **Chat SSE now emits `tool_calling` + `tool_result` events and a real `done.tier`.** The shipped chat stream emitted only `chunk` + `done` and hardcoded `tier: "frontier"`, so the web app couldn't render tool-call cards or a real per-turn tier badge (Spec-09 acceptance #2). `ConversationLoop.turn` gained an optional `on_event` callback (mirroring `AgenticLoop.run`) that surfaces tool calls/results + the router's tier using the **same `RunEvent` shapes as the run-viewer stream** (one event vocabulary for both); `chat_service` maps them to ordered SSE frames before `done`, with `done.tier` = the real choice. `tool_result` is `is_error`+`content` (no `error` field, D-03-3). Added a run-level `RunEvent.tier` constructor. No change to `turn`'s yield contract (the param defaults to `None`). ([`loop.py`](packages/runtime/src/persona_runtime/loop.py), [`events.py`](packages/runtime/src/persona_runtime/agentic/events.py), [`chat_service.py`](packages/api/src/persona_api/services/chat_service.py))
- **Chat now streams delta-by-delta (Spec-05 follow-up; found in browser testing).** `ConversationLoop` drained the model stream and `turn()` yielded the **whole** reply as a single chunk, so acceptance #2's "character-by-character" was only true at the SSE/UI layer — the chat arrived all-at-once. The new `_stream_round` **yields each text delta as it arrives** (into a `_RoundOutcome` accumulator that still captures full text + tool calls + usage for the tool sub-loop + episodic write-back, D-05-13). Regression-tested (`test_streams_text_delta_by_delta`: N deltas → N chunks). The agentic loop is intentionally step-based (D-06-7) and untouched. ([`loop.py`](packages/runtime/src/persona_runtime/loop.py))

## [0.8.0] — 2026-05-28

Spec 08 close-out. The **hosted FastAPI service** (`persona-api`) — the composition root where the sync stores (07), the sync runtime loops (05–06), the toolbox (03), and the backends (02) wire together inside async FastAPI. Auth + RLS, persona CRUD + LLM authoring, SSE streaming chat, background agentic runs, rate limiting, a credits stub, observability, and an auto-generated OpenAPI surface. The headline is the **structural RLS contract** (D-08-1): a per-request engine pool listener sets `app.current_user_id` on every connection from a request-scoped contextvar, so tenant isolation is a property of the engine — not a per-route discipline that could be forgotten. A security-reviewer pass found and the fix closed a HIGH JWT algorithm-confusion bug; the full tenant-boundary review is otherwise clean.

### Added
- `persona_api.app` — the `create_app()` factory + lifespan composition root. The lifespan owns the RLS engine, the embedder, the rate limiter, the agentic-run registry, and the app-scoped `TierRegistry`; on shutdown it calls `await tier_registry.aclose()` + `await client.disconnect()` per MCP client (D-05-4). ([`app.py`](packages/api/src/persona_api/app.py), [`config.py`](packages/api/src/persona_api/config.py))
- `persona_api.middleware.rls_context` — **the structural RLS mechanism (D-08-1).** `make_rls_engine` attaches `checkout`/`checkin` pool listeners that `set_config('app.current_user_id', <uid or ''>, false)` from a request-scoped `contextvar` and reset it — so every connection a request touches (route queries AND the runtime store's own `engine.begin()`) is tenant-scoped, and an absent uid fails closed. Settled by an adversarial spike (Phase 3). ([`rls_context.py`](packages/api/src/persona_api/middleware/rls_context.py))
- `persona_api.auth` — the injectable `verify_token` seam (D-08-4). `python-jose` JWT verification with the key bound to the token's algorithm family (HMAC→secret, RSA/EC→public key) to prevent algorithm-confusion; fail-fast on key/alg mismatch. `get_current_user` sets the RLS contextvar for the request. Tests override the seam with a fake JWT. ([`deps.py`](packages/api/src/persona_api/auth/deps.py))
- `persona_api.schemas` — frozen Pydantic request/response models. The approved connector-agnostic change (D-08-3): an optional nullable `ChannelContext` on the message request + `format_hints` on the SSE `done` event — opaque passthrough the API never branches on. ([`requests.py`](packages/api/src/persona_api/schemas/requests.py), [`responses.py`](packages/api/src/persona_api/schemas/responses.py))
- Routes: personas CRUD + LLM authoring; conversations + SSE chat (KEYSTONE 1); agentic runs start/events/respond/cancel (KEYSTONE 2); `/me/credits` + `/me/usage`; `/v1/tools` + `/v1/skills`; `/healthz`. ([`routes/`](packages/api/src/persona_api/routes/))
- Services: `persona_service` (CRUD + memory-store population on create, D-08-8), `chat_service` (SSE chat, persist-after-final, channel passthrough), `run_service` + `background/run_worker` (in-process `asyncio.Task` runs, per-run event-bus queue, blocking `user_respond`, cancel, per-step persist — D-08-5), `authoring_service`, `credits_service` (deduct-after-success, D-08-6), `audit_service`, `catalog_service`, `runtime_factory` (builds the real loops per request). ([`services/`](packages/api/src/persona_api/services/))
- `persona_api.middleware.rate_limit` — per-user/per-endpoint/per-minute limiter (§6) with in-memory + Postgres stores; `X-RateLimit-*` headers. ([`rate_limit.py`](packages/api/src/persona_api/middleware/rate_limit.py))
- `PostgresTurnLogWriter` (D-08-7) — the spec-05 `TurnLogWriter` Protocol against `turn_logs`, injected into the conversation loop. ([`turn_log_writer.py`](packages/api/src/persona_api/services/turn_log_writer.py))
- Alembic `002_add_message_channel` — the first incremental migration: a nullable `messages.channel` JSONB column (D-08-3). ([`002_add_message_channel.py`](packages/api/alembic/versions/002_add_message_channel.py))

### Changed
- `persona.registry.PersonaRegistry` — added a public `load_persona(persona)` (the string/object-input sibling of `load(path)`) so the API indexes a request-body persona without reaching into private internals; `load(path)` delegates to it. ([`registry.py`](packages/core/src/persona/registry.py))
- `persona.tools.build_default_toolbox` — added an `extra_tools=` parameter so the composition root folds in the `use_skill` tool (D-04-10: not auto-registered) without touching the Toolbox's private state. ([`_factory.py`](packages/core/src/persona/tools/_factory.py))
- `persona-runtime` ships a `py.typed` marker so `persona-api` (standard mypy) can import its types. ([`py.typed`](packages/runtime/src/persona_runtime/py.typed))

### Security
- **HIGH (fixed):** the JWT verifier chose the verification key independently of the algorithm — an RS256 deployment that left `jwt_algorithms` at its HS256 default could be tricked into verifying an HS256 token forged with the (public) RSA key as the HMAC secret (algorithm-confusion). Fixed by binding the key to the token's algorithm family + fail-fast on key/alg mismatch; regression-tested. The full tenant-boundary security-reviewer pass (8 threat classes) is otherwise clean.
- **Open (deployment, → spec 11):** the JWT audience check is skipped when `PERSONA_API_JWT_AUDIENCE` is unset (the v0.1 default, since the auth provider is deferred). Set it in production once a provider is chosen.

## [0.7.0] — 2026-05-28

Spec 07 close-out. The production storage layer: a `PostgresBackend` transport (in `persona-core`, behind the `[postgres]` extra) plus the full SQL schema, Alembic migration, and row-level-security policies (in `persona-api`). The headline architectural decision is that there is **no** standalone `PostgresPGVectorStore` — the existing four typed stores compose the new transport unchanged, so policy/versioning/audit/decay are reused, not re-implemented. The `MemoryStore` protocol stays synchronous (D-07-1, psycopg3 sync); the spec's async §4 sketch was superseded.

### Added
- `persona.stores.backend.Backend` — a `@runtime_checkable` transport protocol (`upsert`/`query`/`get_all`/`delete_persona`/`delete_documents`) extracted from `ChromaBackend`'s real surface. `TypedStore` now composes any `Backend`, so Chroma and Postgres are Liskov-interchangeable (D-07-3). ([`backend.py`](packages/core/src/persona/stores/backend.py))
- `persona.stores.postgres.PostgresBackend` — the production transport (psycopg3 sync + SQLAlchemy Core + pgvector). Embeds at write via an injected `Embedder`, asserts the embedding dim is exactly 384 (fail-fast), round-trips `ChunkProvenance` through promoted columns, and populates `chunk.distance` from the cosine `<=>` operator. **No decay SQL** — `EpisodicStore`'s Python-side `exp(-elapsed/tau)` (D-01-4) re-rank is reused, giving automatic Chroma parity (the spec's §4.3 decay SQL is superseded). ([`postgres.py`](packages/core/src/persona/stores/postgres.py))
- `persona_api.db.models` — the canonical SQLAlchemy Core schema (11 tables). `memory_chunks` promotes the versioning/provenance fields (`logical_id`/`version`/`superseded_by` + `content_hash` + the `ChunkProvenance` fields) to indexed columns (D-07-4); user metadata lives in a `metadata` JSONB column; identity chunks store NULL provenance. Indexes: `(persona_id, kind)`, `(persona_id, kind, logical_id)`, a partial `WHERE superseded_by IS NULL` current-heads index, and an HNSW `vector_cosine_ops` index. Composite FK `(persona_id, owner_id) → personas` on `conversations`/`runs` (defence-in-depth, security finding 1). ([`models.py`](packages/api/src/persona_api/db/models.py))
- `persona_api.db.engine` — `create_db_engine`, `set_current_user`, and the `rls_connection` context manager. The RLS user id is set via `set_config('app.current_user_id', :uid, true)` (parameterised, transaction-local) — **not** `SET LOCAL ... = :uid`, which is a syntax error with a bound param (D-07-5, verified by spike). ([`engine.py`](packages/api/src/persona_api/db/engine.py))
- `persona_api.db.rls` — per-table RLS policy SQL with the correct FK-chain joins (personas/conversations/runs direct `owner_id`; messages/turn_logs → conversations; memory_chunks → personas; credits/credit_transactions → `user_id`). `ENABLE` + `FORCE ROW LEVEL SECURITY`; fail-closed `current_setting(...,true)`; `WITH CHECK` mirrors `USING`. ([`rls.py`](packages/api/src/persona_api/db/rls.py))
- `persona-api` Alembic env + `001_initial` migration — synchronous runner (`postgresql+psycopg://`), reads `DATABASE_URL`. `001_initial` creates the extension, all tables/indexes, and RLS policies in one atomic upgrade (RLS in `001`, no unsafe window). Real `downgrade`. ([`env.py`](packages/api/alembic/env.py), [`001_initial.py`](packages/api/alembic/versions/001_initial.py))

### Changed
- `persona.stores.base.TypedStore.__init__` — `backend` parameter widened from `ChromaBackend` to the `Backend` protocol (additive; the spec-01 store regression suite stays green). `delete()` calls the storage-neutral `delete_persona` (D-07-3). ([`base.py`](packages/core/src/persona/stores/base.py))
- `persona.stores.chroma.ChromaBackend` — `delete_collection` renamed to `delete_persona` (the Chroma `delete_collection` SDK call stays internal). ([`chroma.py`](packages/core/src/persona/stores/chroma.py))
- `packages/core/pyproject.toml` — the `[postgres]` extra swapped from `asyncpg`+`sqlalchemy[asyncio]` to `psycopg[binary]`+`sqlalchemy` (sync; D-07-1).
- Root `pyproject.toml` — depends on `persona-core[postgres]` so a plain `uv sync` installs the extra; added a mypy override ignoring `pgvector.*` missing stubs.
- `.env.example` / `alembic.ini` — `DATABASE_URL` dialect changed `postgresql+asyncpg://` → `postgresql+psycopg://`.
- `docker-compose.yml` — dropped the obsolete `version:` key.

### Security
- Row-level security on every tenant-scoped table; tenant isolation proven by adversarial integration tests (cross-tenant query returns zero rows; `WITH CHECK` blocks cross-tenant writes; fail-closed when the user GUC is unset). A `security-reviewer` pass surfaced a defence-in-depth gap (a tenant could attach a conversation/run to another tenant's persona via the single-column FK, even though RLS hid the row) — closed with the composite `(persona_id, owner_id)` FK and a regression test.

## [0.6.0] — 2026-05-28

Spec 06 close-out. The agentic loop (`persona_runtime.agentic`) — the plan-act-reflect execution engine for end-to-end tasks ("draft a complaint about my landlord refusing to fix mould"). Pure orchestration over specs 01–05; zero new dependencies. The simplest possible agent loop (architecture §5.2): one model decides at each step whether to call a tool, ask the user, or produce a final answer.

### Added
- `persona_runtime.agentic.errors` — `MaxStepsReachedError`, `RunCancelledError` (under `PersonaError`). Defined for spec-08's optional use; the loop itself returns a `Run` with a terminal `RunStatus` rather than raising (D-06-2). ([`errors.py`](packages/runtime/src/persona_runtime/agentic/errors.py))
- `persona_runtime.agentic.step` — `StepType` (StrEnum) + `Step` (frozen Pydantic). A step records its action, tool calls/results, question/answer, content, and per-step telemetry (`tier_used`/`tokens`/`latency_ms` — the v0.1 telemetry sink; no separate `StepLog` writer, D-06-3). ([`step.py`](packages/runtime/src/persona_runtime/agentic/step.py))
- `persona_runtime.agentic.run` — `RunStatus` (StrEnum) + `Run` (frozen Pydantic, UUID default id, tz-aware datetimes, JSON-serialisable per acceptance #10) + `CancelToken` (plain mutable control class — D-06-1). The loop holds mutable working state and emits the frozen `Run` at the end. ([`run.py`](packages/runtime/src/persona_runtime/agentic/run.py))
- `persona_runtime.agentic.events` — `RunEvent` (frozen Pydantic) + 12 typed classmethod constructors (`started`/`thinking`/`tool_calling`/`tool_result`/`asking_user`/`user_responded`/`reasoning`/`completed`/`cancelled`/`max_steps`/`error`/`finished`). The single place each event's `type`+`data` payload is defined; the API serialises these to SSE (§8). ([`events.py`](packages/runtime/src/persona_runtime/agentic/events.py))
- `persona_runtime.agentic.compactor.StepHistoryCompactor` — compacts step history at 80% of the tier budget (§6). Preserves the persona block + task (the floor, `context[0]`) and the recent tail verbatim (acceptance #8). The async-bridge is kept local (no shared `_bridge.py`): the loop pre-computes the small-tier summary and passes the compactor a resolved string (D-06-4). ([`compactor.py`](packages/runtime/src/persona_runtime/agentic/compactor.py))
- `persona_runtime.agentic.loop.AgenticLoop` — the keystone. `async run(task, on_event, user_respond, cancel_token) -> Run` runs the plan-act-reflect cycle: non-streaming `chat()` per step; classification via `[ASK_USER]`/`[FINAL]` markers + a question-mark heuristic fallback (no classifier); error recovery (a hallucinated/failed tool feeds back `ToolResult(is_error=True, ...)`, D-03-3; same bad name twice → a stronger instruction, §5.2); boundary-only cancellation (D-06-7); a best-effort frontier summary at `max_steps` (status `max_steps_reached`, never `completed` — D-06-2); the `use_skill` intercept (D-04-10); step-tier policy in the loop (`_tier_for_step` + a `force_frontier_tier` escape hatch, D-06-6); and an end-of-run episodic write tagging the chunk as a skill candidate for a future spec 13 (`source=agentic_run` + run/task/tools/steps/status metadata, D-06-8). `max_steps` default 20; no inner per-step tool-round cap (D-06-7). ([`loop.py`](packages/runtime/src/persona_runtime/agentic/loop.py))
- `persona_runtime.agentic.__init__` re-exports the public surface: `AgenticLoop`, `Run`, `RunStatus`, `Step`, `StepType`, `RunEvent`, `CancelToken`, `StepHistoryCompactor`, `MaxStepsReachedError`, `RunCancelledError`. ([`__init__.py`](packages/runtime/src/persona_runtime/agentic/__init__.py))

### Changed
- `packages/core/SPEC.md` — added an "Agentic loop (Spec 06)" subsection.
- `.env.example` — noted `max_steps` is a constructor default (20), no env knob.
- No new dependencies — spec 06 is orchestration over existing surfaces.

## [0.5.0] — 2026-05-28

Spec 05 close-out. `persona-runtime` — the conversation loop, prompt builder, router, tier registry, and per-turn logging. The first integration spec; composes specs 01–04 into a runnable turn loop. First code outside `persona-core`.

### Added
- `persona_runtime.errors.TierNotConfiguredError` — the one new runtime domain exception (D-05-2); everything else re-raises spec-01/02/03 domain exceptions unchanged (hexagonal). ([`errors.py`](packages/runtime/src/persona_runtime/errors.py))
- `persona_runtime.tier` — `TierConfig` (frozen dataclass) + `TierRegistry` (lazy-instantiate + cache via `load_backend`; `small→mid→frontier` fallback; single-backend fallback from `PERSONA_*`; `TierNotConfiguredError` if nothing resolves). `aclose()` duck-types backend cleanup (`getattr(backend, "aclose"/"disconnect")`) and is owned by the composition root, not the loop (D-05-3, D-05-4). `tier_registry_from_env()` presence-checks `<PREFIX>PROVIDER`. ([`tier.py`](packages/runtime/src/persona_runtime/tier.py))
- `persona_runtime.router.Router` — rule-based, no ML (architecture §5.3). Precedence: per-persona override → first-turn-frontier → boilerplate-small → persona-critical-frontier → mid default. `_is_persona_critical` derives keywords per-call from the persona's constraints + worldview (D-05-5); word-boundary regex. ([`router.py`](packages/runtime/src/persona_runtime/router.py))
- `persona_runtime.prompt` — `RetrievedContext` (frozen Pydantic bundle) + `PromptBuilder.build(...)`. System block in spec §5.1 order (identity → constraints → self-facts → worldview → episodic → skill index → active skill content → footer); worldview epistemic tags in parentheses. Receives already-budgeted `matched_skill_content: str` — no `SKILL_TOKEN_BUDGET` on the builder (the `SkillInjector` owns the 2000 budget; D-05-7). Context-window reduction drops episodic → worldview → self-facts; identity/constraints/skill-index are the never-truncated floor (spec §5.3). Token estimate via `persona.skills.count_tokens` (D-05-8). ([`prompt.py`](packages/runtime/src/persona_runtime/prompt.py))
- `persona_runtime.logging` — `TurnLog` (frozen Pydantic, not the spec's `@dataclass`; crosses the spec-08 Postgres boundary, D-05-9) + `TurnLogWriter` Protocol + `JSONLTurnLogWriter` (path mirrors D-01-6 audit convention; `PERSONA_TURNLOG_PATH` override) + `MemoryTurnLogWriter`. `_PRICE_TABLE` + `estimate_cost_cents` — hand-maintained estimate; unknown `(provider, model)` → `0.0` + warn-once (S05-3, D-05-10). ([`logging.py`](packages/runtime/src/persona_runtime/logging.py))
- `persona_runtime.loop.ConversationLoop` — the keystone. `async turn(conversation, user_message) -> AsyncIterator[StreamChunk]` runs the full spec §4.1 sequence. The sync/async summariser bridge (D-05-X): predicts compaction via `_will_compact` (replicating `manage()`'s boundary math, cross-checked by a lockstep test), pre-computes the small-tier summary, hands `manage()` a sync no-op assembler — never `asyncio.run()` in a sync callable. Unified `max_tool_rounds` counter for tool + use_skill re-prompts (one increment per round, D-05-11); use_skill intercept on `result.data["skill_name"]` with once-per-turn injection. Tool-call reconstruction from streamed `ToolCallDelta`s by `call_id` (D-05-13). Episodic write-back is the last step before the final chunk; a partially-consumed turn writes nothing (async-generator suspend, D-05-12). The loop receives the `Conversation`, never owns it (D-S05-4). ([`loop.py`](packages/runtime/src/persona_runtime/loop.py))
- `persona_runtime.__init__` re-exports the public surface: `ConversationLoop`, `PromptBuilder`, `RetrievedContext`, `Router`, `TierConfig`, `TierRegistry`, `tier_registry_from_env`, `TurnLog`, `TurnLogWriter`, `JSONLTurnLogWriter`, `MemoryTurnLogWriter`, `TierNotConfiguredError`. ([`__init__.py`](packages/runtime/src/persona_runtime/__init__.py))

### Changed
- `packages/runtime/pyproject.toml` — added `tiktoken>=0.7,<1` as a direct dependency (already transitive via `persona-core`; declared directly per engineering standards §5). No other new dependencies — spec 05 is pure orchestration.
- `packages/core/SPEC.md` — added a "Runtime (Spec 05)" subsection (the runtime is a separate consumer package; the dependency arrow points one way).
- `.env.example` — documented the tier-fallback semantics and added `PERSONA_TURNLOG_PATH`.

### Tests
- **96 new runtime tests** (7 errors + 12 tier + 25 router + 9 prompt + 14 logging + 19 loop + 5 integration + 5 end-to-end/context). Two are load-bearing: the boundary-prediction lockstep (loop `_will_compact` vs real `manage()` across K-1/K/K+1 × 3 configs) and the early-consumer-exit episodic-skip (acceptance #10).
- One `python-reviewer` pass on `loop.py`: one valid finding (round counter was incremented per-call, not per-round) fixed + regression-tested; the reviewer's other findings were verified non-bugs against the `manage()` source.
- Runtime test tree intentionally has **no `__init__.py`** (adding them collides `tests.conftest`/`tests.unit` with the core package); a `tests/conftest.py` puts the shared `_fakes` helper on `sys.path`.

### Documentation
- `docs/specs/spec_05/{spec_05_runtime.md, spec_05_kickoff.md, tasks.md, tasks.yaml, research.md, decisions.md, state.md, handover.md, README.md, closeout.md}` — full lifecycle of Spec 05 captured.
- D-05-1..D-05-13 + D-05-X + D-S05-4 added to root [`docs/DECISIONS.md`](docs/DECISIONS.md).

## [0.4.0] — 2026-05-27

Spec 04 close-out. Skills layer — scanner, injector, index renderer, `use_skill` synthetic activation tool, two built-in skill packs.

### Added
- `persona.skills/` package — skill scanner, injector, index renderer, `use_skill` synthetic tool. ([`packages/core/src/persona/skills/`](packages/core/src/persona/skills/))
- `persona.skills._tokens.count_tokens` — wraps `tiktoken cl100k_base` at module import; hard-imported (no `len // 4` fallback per D-04-2). Single module-level `_ENCODER` singleton; thread-safe. ([`skills/_tokens.py`](packages/core/src/persona/skills/_tokens.py))
- `persona.skills._frontmatter.parse_skill_markdown` — hand-rolled ~25-LOC YAML front-matter parser (D-04-3; declines `python-frontmatter` due to BOM silent-failure gap found in Phase 3 §2 research). Tolerates UTF-8 BOM and CRLF line endings; distinguishes all malformed cases with typed `SkillManifestError`. ([`skills/_frontmatter.py`](packages/core/src/persona/skills/_frontmatter.py))
- `persona.errors.SkillManifestError` — raised by the front-matter parser on malformed input. Includes `context["path"]` always and `context["reason"]` on YAML parse failures. ([`errors.py`](packages/core/src/persona/errors.py))
- `persona.skills.scanner.SkillScanner` — `scan(declared_skills, *, tool_allow_list)` with per-skill warn-and-skip envelope catching `SkillManifestError`, `ValidationError`, `KeyError` for missing required front-matter fields, and broad `Exception` (D-04-4); `BaseException` propagates. Silent skip on absent user `skills/` dir (D-04-5); same-name override of a built-in is WARNING-logged. Output preserves declared order. ([`skills/scanner.py`](packages/core/src/persona/skills/scanner.py))
- `persona.skills.index.render_skill_index(skills) -> str` — pure function producing the always-injected compact "available skills" Markdown block (D-04-6). Empty list returns empty string (no header). `when_to_use=None` skips the "Use when:" sub-line. ([`skills/index.py`](packages/core/src/persona/skills/index.py))
- `persona.skills.injector.SkillInjector` — `TOKEN_BUDGET = 2000` class constant (D-04-7, non-negotiable per architecture §5.1.2); `async inject(skill)` with verbatim pass-through / summariser-call / binary-search-truncation branches. Defensive: summariser returning over-budget output falls through to truncation. `MARKER = "\n\n[truncated]"`, ceil-bisection on character index (D-04-8); 16 tokeniser calls for 85 KB body. ([`skills/injector.py`](packages/core/src/persona/skills/injector.py))
- `persona.skills.use_skill_tool.make_use_skill_tool(skills)` — closure-based factory producing the synthetic `use_skill` `AsyncTool` via spec-03's `@tool` decorator unchanged (Pattern-1 activation; D-04-9). On valid skill name: returns `ToolResult(is_error=False, data={"skill_name": "X"})` for runtime interception. On unknown name: returns `ToolResult(is_error=True)` with sorted comma-joined available list. Exported from `persona.skills`, NOT auto-registered in `build_default_toolbox` (D-04-10; spec 05 composes). For non-native-tool backends, spec-02's prompt-shim JSON-block format `{"tool": "use_skill", "args": {...}}` (D-02-6) IS the activation channel — no new wire format. ([`skills/use_skill_tool.py`](packages/core/src/persona/skills/use_skill_tool.py))
- `persona.schema.skills.SkillSpec` extended additively (D-04-1) with `tools_required: list[str]`, `content: str`, `content_token_count: int` — all optional with defaults (`list()`, `""`, `0` respectively). Spec-01's four-field construction surface unchanged. ([`schema/skills.py`](packages/core/src/persona/schema/skills.py))
- Built-in skill packs `web_research` (2,804 tokens, exercises the over-budget injector path end-to-end) and `document_drafting` (1,151 tokens, exercises the verbatim pass-through path) under `persona/skills/builtin/`. Both regression-guarded by `tests/integration/test_builtin_skills.py::TestTokenCountRegressionGuards`. ([`skills/builtin/web_research/SKILL.md`](packages/core/src/persona/skills/builtin/web_research/SKILL.md), [`skills/builtin/document_drafting/SKILL.md`](packages/core/src/persona/skills/builtin/document_drafting/SKILL.md))
- `persona.skills.__init__` re-exports seven public names: `SkillSpec`, `SkillScanner`, `SkillInjector`, `render_skill_index`, `make_use_skill_tool`, `count_tokens`, `SkillManifestError`. ([`skills/__init__.py`](packages/core/src/persona/skills/__init__.py))

### Changed
- `tiktoken` dependency status changes from "parked" (D-01-11) to "live" — used by `persona.skills._tokens` for skill-content token counting. No version-pin change; `tiktoken>=0.7,<1` was already in core deps.
- `packages/core/SPEC.md` — added "Skills (Spec 04)" subsection summarising the package structure, public guarantees, and the seven D-04 decisions. `tiktoken` Dependencies-comment updated from "parked; spec 05 (prompt builder)" to "live in spec 04 (skill token-budget enforcement)".
- `.env.example` — added an informational comment block in the new "Skills (spec 04)" section noting that `TOKEN_BUDGET` is a module constant (no env knob in v0.1 per D-04-7) and that absent user `skills/` directories are silently skipped (D-04-5).

## [0.3.0] — 2026-05-27

Spec 03 close-out. Tools, MCP, and the Toolbox.

### Added
- `persona.tools.ToolDescriptor` Protocol (the metadata surface — `name`, `description`, `parameters_schema`) and `persona.tools.AsyncTool` Protocol (extends `ToolDescriptor` with `async execute(**kwargs) -> ToolResult`). Sibling to spec-01's sync `Tool` Protocol (D-03-2; spec-01's `Tool` is untouched). ([`tools/protocol.py`](packages/core/src/persona/tools/protocol.py))
- `@tool(name=..., description=...)` decorator wrapping an `async def` into an `AsyncTool`. JSON Schema synthesised via `pydantic.TypeAdapter`; argument model uses `ConfigDict(extra="forbid")` so typo'd kwargs from the model fail validation. Two catch sites — argument-validation errors AND body-raised `Exception` (not `BaseException`) — both produce `ToolResult(is_error=True, ...)`. `BaseException` propagates (D-03-5). ([`tools/protocol.py`](packages/core/src/persona/tools/protocol.py))
- `persona.tools.Toolbox` — registry + literal-only allow-list + async `dispatch`. `None` allow-list is permissive with a WARNING log (development convenience per D-03-7); production callers pass `persona.tools`. Duplicate tool names raise `ValueError`. `ToolNotAllowedError.context["allowed"]` carries a comma-joined string of available names per D-03-8. ([`tools/toolbox.py`](packages/core/src/persona/tools/toolbox.py))
- `format_tool_result(call, result, *, provider_name) -> ConversationMessage` — provider-aware formatter using a `match` statement on seven supported provider names. Anthropic (`tool_result` content block in user message), OpenAI / DeepSeek / Groq / Together (role=tool with `tool_call_id`), Ollama / local HF (shim plain-text). Unknown provider raises `ValueError` (D-03-6). ([`tools/formatting.py`](packages/core/src/persona/tools/formatting.py))
- Built-in tool `web_search` (D-03-9, D-03-10) — `make_web_search_tool(provider, api_key, http)` factory; `_SearchProvider` Protocol; `BraveSearchProvider` wired against `https://api.search.brave.com/res/v1/web/search` with `X-Subscription-Token` header; `TavilySearchProvider` and `SerpAPISearchProvider` raise `NotImplementedError` (caught by the `@tool` envelope → `ToolResult(is_error=True)`). Provider via `PERSONA_WEB_SEARCH_PROVIDER`; key via `PERSONA_WEB_SEARCH_API_KEY`. Structured results in `ToolResult.data["results"]`. ([`tools/builtin/web_search.py`](packages/core/src/persona/tools/builtin/web_search.py))
- Built-in tool `web_fetch` (D-03-11, D-03-12, D-03-24) — `httpx` + `trafilatura.extract(output_format="txt", favor_precision=True, include_comments=False, include_tables=False)`. Non-HTML content-type passes through via `Response.text`. Truncation past `max_chars` sets `truncated=True` + `data["original_length"]`. Scheme allow-list: `http`/`https` only; full SSRF guard deferred to spec 11. ([`tools/builtin/web_fetch.py`](packages/core/src/persona/tools/builtin/web_fetch.py))
- Sandbox path resolver `persona.tools._sandbox.resolve_sandbox_path(root, requested) -> Path` — pure function, no I/O. Rejects: NULL byte (D-03-15), >4096-char paths, mixed `\\` separator on POSIX, empty/whitespace, absolute paths (`PurePosixPath.is_absolute`), `.` / `./` root references, paths whose `.resolve(strict=False)` escapes `root.resolve()` (catches `..` traversal AND symlink escape). 55 adversarial tests written tests-first (Phase 1 refinement #8); two `security-reviewer` subagent passes (T09 + T10) with all findings addressed. `_preview()` strips control characters from user input before embedding in error context (security-review T09 Finding 1). ([`tools/_sandbox.py`](packages/core/src/persona/tools/_sandbox.py))
- Built-in tools `file_read` + `file_write` (D-03-16, D-03-17, D-03-18) — `make_file_read_tool(sandbox_root)` and `make_file_write_tool(sandbox_root, audit_logger, persona_id)` factories. `os.open(O_NOFOLLOW | ...)` closes the TOCTOU window between resolver and open. UTF-8 with `errors="replace"` for reads; 1 MB cap with `truncated=True` over. `file_write` mode `0o600`, emits one `ToolAuditEvent(action="write")` per successful write. Lone-surrogate `UnicodeEncodeError` and `os.write` `OSError` both caught and returned as clean `ToolResult(is_error=True, ...)` (security-review T10 Findings 5 + 10.2). ([`tools/builtin/file_read.py`](packages/core/src/persona/tools/builtin/file_read.py), [`tools/builtin/file_write.py`](packages/core/src/persona/tools/builtin/file_write.py))
- MCP client + adapter (D-03-19, D-03-20, D-03-21) — `mcp.client.streamable_http.streamablehttp_client` transport (NOT the deprecated `mcp.client.sse`). `MCPClient` uses `AsyncExitStack` for procedural-style lifecycle (`await client.connect()` / `disconnect()`). `MCPToolAdapter` wraps each discovered MCP tool as an `AsyncTool` named `mcp:<server>:<tool>` (literal allow-list per Phase 1 refinement #4). Graceful degradation `strict=False` for Toolbox auto-load. Audit events on connect / disconnect / server_unavailable; per-call dispatch audits skipped. Disconnection-like errors → `ToolResult(is_error=True, content="MCP server disconnected")`. `load_mcp_clients(servers, ...)` helper. ([`tools/mcp/client.py`](packages/core/src/persona/tools/mcp/client.py), [`tools/mcp/adapter.py`](packages/core/src/persona/tools/mcp/adapter.py))
- `persona.tools.audit` — dedicated tool-audit port (D-03-25, supersedes D-03-18's "reuse `AuditEvent`" recap). `ToolAuditEvent` Pydantic v2 model + `ToolAuditLogger(Protocol)` + `JSONLToolAuditLogger` / `MemoryToolAuditLogger` implementations. The JSONL logger documents single-process safety (security-review T10 Finding 7); hosted-service multi-process safety lands with the Postgres backend in spec 08. ([`tools/audit.py`](packages/core/src/persona/tools/audit.py))
- `build_default_toolbox(config, persona, *, tool_audit_logger) -> tuple[Toolbox, list[MCPClient]]` — composes the four built-in tools + connects MCP servers from `PersonaCoreConfig.mcp_servers_parsed`. Returns the toolbox and the MCP clients (so the caller can `await client.disconnect()` on shutdown). Graceful degradation per D-03-20. ([`tools/_factory.py`](packages/core/src/persona/tools/_factory.py))
- Two new domain exceptions: `MCPConnectionError`, `MCPServerUnavailableError` — flat under `PersonaError` per D-03-1. Re-exported from `persona.tools.errors`. ([`errors.py`](packages/core/src/persona/errors.py), [`tools/errors.py`](packages/core/src/persona/tools/errors.py))
- `persona.tools.__init__` re-exports 22 names — Protocols, `Toolbox`, `@tool`, formatter, the four built-in factories, MCP client + adapter, `build_default_toolbox`, audit Protocol + impls + event, and the five tool/MCP exceptions.

### Changed
- `persona.schema.tools.ToolResult` additively extended with `data: dict[str, Any] | None = None` and `truncated: bool = False` (D-03-3). `extra="forbid"` enforces that there is no separate `error` field — `is_error=True` + `content` is the single failure-truth.
- `persona.backends.types.tool_spec_from_tool()` parameter widened from `Tool` to `ToolDescriptor` — strictly additive (every `Tool` is a `ToolDescriptor`; every `AsyncTool` is too). No breaking change to spec-02's call sites.
- `PersonaCoreConfig` gained four spec-03 fields: `web_search_provider: Literal["brave", "tavily", "serpapi"]`, `web_search_api_key: SecretStr | None`, `tools_sandbox_root: Path` (default `./.persona_work` per D-03-23), `mcp_servers: str` (raw env value; the parsed dict is exposed via the `mcp_servers_parsed` property because Pydantic Settings JSON-pre-parses `dict[str, str]` env vars before validators run). ([`config.py`](packages/core/src/persona/config.py))
- `packages/core/pyproject.toml` — added `trafilatura>=2.0,<3` (web_fetch) and `mcp>=1.0,<2` (MCP client). Both core deps per D-03-12; transitive trees documented in [`docs/specs/spec_03/research.md`](docs/specs/spec_03/research.md) §2-3.
- `.env.example` — renamed `PERSONA_SEARCH_*` → `PERSONA_WEB_SEARCH_*` per Phase 1 refinement #7 (futureproofs against vector/code search later); added `PERSONA_TOOLS_SANDBOX_ROOT` and `PERSONA_MCP_SERVERS`.
- `packages/core/SPEC.md` — "Tools, MCP, and the Toolbox (Spec 03)" subsection added.

### Tests
- **214 new unit tests** across `tests/unit/tools/` (11 errors + 18 protocol + 20 decorator + 36 formatting + 20 toolbox + 17 web_search + 16 web_fetch + 55 sandbox + 30 file tools + 12 MCP adapter + 11 MCP client + 22 factory/config).
- Two `security-reviewer` subagent passes: T09 (sandbox resolver, 4 findings) + T10 (file tools, 10 findings — 1 HIGH, 2 MEDIUM, others LOW/accepted-risk). All actionable findings addressed in code; accepted-risk findings documented for spec 11.
- **682 unit + 28 integration + 26 contract = 736 total tests, all green.**
- All checks: `ruff check`, `ruff format --check`, `mypy --strict packages/core/src` clean (61 source files; was 47 after spec 02).

### Documentation
- `docs/specs/spec_03/{spec_03_tools.md, spec_03_kickoff.md, tasks.md, tasks.yaml, research.md, decisions.md, state.md, handover.md, README.md, closeout.md}` — full lifecycle of Spec 03 captured.
- D-03-1..D-03-25 added to root [`docs/DECISIONS.md`](docs/DECISIONS.md).

## [0.2.0] — 2026-05-27

Spec 02 close-out. Model backends and provider abstraction.

### Added
- `persona.backends.ChatBackend` async Protocol with `chat()` (single-shot) + `chat_stream()` (`AsyncIterator[StreamChunk]`). ([`backends/protocol.py`](packages/core/src/persona/backends/protocol.py))
- `OpenAICompatibleBackend` — unified backend for Anthropic (via `anthropic` SDK) and OpenAI / DeepSeek / Groq / Together (via `openai.AsyncOpenAI` with per-provider `base_url`). Native tool calling where the provider supports it; prompt-based JSON-block shim fallback. ([`backends/openai_compat.py`](packages/core/src/persona/backends/openai_compat.py))
- `OllamaBackend` — raw `httpx` to a local Ollama instance at `/api/chat`; lazy client; opt-in native tools (`use_native_tools=True`); explicit `ping()` health check; `aclose()` for lifecycle. ([`backends/ollama.py`](packages/core/src/persona/backends/ollama.py))
- `HFLocalBackend` behind `persona-core[local]` extras — lazy weight load via `asyncio.Lock`-guarded `_ensure_loaded()`; 4-bit NF4 / 8-bit / fp16 quantisation; Gemma-2 system-role fold + eager attention; `generation_config` override; `AsyncTextIteratorStreamer` for async streaming with `_CancellableStoppingCriteria`. ([`backends/hf_local.py`](packages/core/src/persona/backends/hf_local.py))
- Five new domain exceptions: `ProviderError`, `AuthenticationError`, `RateLimitError`, `ModelNotFoundError`, `BackendTimeoutError` — all subclasses of `PersonaError`, carry structured `context` per the engineering standards. ([`backends/errors.py`](packages/core/src/persona/backends/errors.py))
- Prompt-based tool-calling shim (`{"tool": "name", "args": {...}}` JSON blocks) with fail-safe parser (D-02-14). ([`backends/_tool_shim.py`](packages/core/src/persona/backends/_tool_shim.py))
- `BackendConfig` (Pydantic Settings, `PERSONA_*` env-only) with `from_env(prefix=...)` for tier-specific overrides (used by spec 05). ([`backends/config.py`](packages/core/src/persona/backends/config.py))
- `load_backend(BackendConfig)` factory + `persona.backends` package re-exports. ([`backends/__init__.py`](packages/core/src/persona/backends/__init__.py), [`backends/_factory.py`](packages/core/src/persona/backends/_factory.py))
- Response types: `ChatResponse`, `StreamChunk`, `TokenUsage`, `ToolSpec`, `ToolCallDelta` — Pydantic v2 frozen + `extra="forbid"` (D-02-2). `tool_spec_from_tool()` helper bridges spec-01's `Tool` Protocol. ([`backends/types.py`](packages/core/src/persona/backends/types.py))
- CLI: `persona chat` now wires through `load_backend(BackendConfig())` and streams via `chat_stream()`; `EchoBackend` placeholder deleted (D-02-12). ([`cli/chat_cmd.py`](packages/core/src/persona/cli/chat_cmd.py))
- Test helper `MockChatBackend` in `tests/_mock_backend.py` for CLI / integration tests (replaces deleted `_echo.py`).
- Contract test suite ([`tests/contract/test_chat_backend_contract.py`](packages/core/tests/contract/test_chat_backend_contract.py)) — 26 parametrised tests across 4 backend variants verifying Protocol compliance, chat shape, streaming, fail-fast auth, and tool-call round-trip.

### Changed
- `packages/core/pyproject.toml` — added `anthropic>=0.30,<1` and `openai>=1.30,<2` as core dependencies; `httpx>=0.27,<1` (parked under D-01-11) now live.
- `.env.example` — added `PERSONA_PROVIDER`, per-provider key vars, `PERSONA_BASE_URL`, `PERSONA_REQUEST_TIMEOUT_S`, `PERSONA_DOTENV_LOAD`, and HF local vars.
- `packages/core/SPEC.md` — model backends subsection added.

### Removed
- `packages/core/src/persona/cli/_echo.py` (deleted per D-02-12). Production no longer ships a fake backend; tests inject their own.

### Tests
- 414 unit (was 210; +204 new in `tests/unit/backends/`) + 28 integration + 26 contract = **468 total green**.
- New file: `tests/contract/test_chat_backend_contract.py` runs the same assertions against every backend variant.
- All checks: `ruff check`, `ruff format --check`, `mypy --strict packages/core/src` clean (47 source files).

### Documentation
- `docs/specs/spec_02/{spec_02_backends.md, tasks.md, tasks.yaml, research.md, decisions.md, state.md, handover.md, README.md, closeout.md}` — full lifecycle of Spec 02 captured.
- D-02-1..D-02-18 added to root [`docs/DECISIONS.md`](docs/DECISIONS.md).

## [0.1.0] — 2026-05-27

First spec close-out. Foundation of `persona-core`.

### Added
- v1.0 persona YAML schema (Pydantic v2, frozen, `extra="forbid"`) covering identity, self-facts, worldview, episodic, routing, embedding, tools, skills. ([`schema/persona.py`](packages/core/src/persona/schema/persona.py))
- `PersonaChunk` with deterministic SHA-256 `content_hash`, tz-aware UTC datetimes, and `ChunkProvenance` for the version chain. ([`schema/chunks.py`](packages/core/src/persona/schema/chunks.py))
- Three-source persona update model (`system` / `user` / `persona_self`) with per-store policy table. Versioned append-only updates with `history` and `rollback`. ([`stores/policy.py`](packages/core/src/persona/stores/policy.py), [`stores/versioning.py`](packages/core/src/persona/stores/versioning.py))
- `MemoryStore` protocol + four concrete typed stores: `IdentityStore`, `SelfFactsStore`, `WorldviewStore`, `EpisodicStore`. Episodic decay is query-time exponential (`tau=24h` default).
- `ChromaMemoryStore` transport with deterministic per-`(persona, store_kind)` collection naming, cosine-distance HNSW, SQLite query-batch cap, and provenance serialised into Chroma metadata.
- `PersonaRegistry` — load YAML, validate, index author-time chunks; idempotent re-load.
- `ConversationHistoryManager` — summarise-and-compact (`compact_every=10`, `keep_recent=5`). Summariser injected.
- Per-component logging via `loguru` (`persona.logging.get_logger`), idempotent sink configuration (D-01-7).
- JSONL audit log behind an `AuditLogger` Protocol; every store mutation emits exactly one `AuditEvent`. (`MemoryAuditLogger` for tests.)
- Typer CLI: `persona init`, `persona validate`, `persona chat` (placeholder `EchoBackend`), `persona audit`, `persona run` (stub for spec 06).
- `py.typed` marker shipped in the wheel; structured-context domain exceptions; CHANGELOG; .editorconfig; pre-commit hooks (ruff, ruff-format, mypy --strict, pytest --collect-only).

### Infrastructure
- Root `pyproject.toml` declares workspace members as root dependencies so a plain `uv sync` installs the whole monorepo.
- `[tool.uv.sources]` blocks in `packages/runtime/pyproject.toml` and `packages/api/pyproject.toml` (required by uv).
- Root `conftest.py` prepends each workspace `src/` to `sys.path` to work around CPython 3.13's hidden-`_editable_impl_` `.pth` skip.

### Tests
- 210 unit + 28 integration tests across 11 test files. 10 valid + 10 invalid persona YAML fixtures. Pure-function policy table tested in isolation; concrete stores tested against real ChromaDB.

### Documentation
- `docs/specs/spec_01/{spec_01_core.md, tasks.md, tasks.yaml, research.md, decisions.md, state.md, handover.md, README.md, closeout.md}` — full lifecycle of Spec 01 captured.
