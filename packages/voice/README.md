# persona-voice

> The real-time voice trunk for Open Persona — LiveKit WebRTC transport, streaming STT/TTS, turn-taking, and persona-conditioned generation.

`persona-voice` is the voice layer of the [Open Persona](../../README.md)
stack: a real-time, full-duplex voice surface that layers sub-second-latency
audio onto the **same** persona, typed memory, and tier-routed runtime the text
stack uses. The voice persona *is* the persona — never a thin prompt bypass.

---

## What it is / where it fits

`persona-voice` runs **in-process with `persona-core`** (no separate language,
no cross-process IPC) so the typed-memory stores, audit log, and credits
service compose directly. From V5 it also composes
[`persona-runtime`](../runtime/README.md) (prompt builder, router, shared
retrieval) so a voice turn is conditioned exactly like a text turn. The
layering stays acyclic: **voice → runtime → core**; runtime never imports voice.

WebRTC transport is provided by a **LiveKit OSS** substrate. The browser joins
a LiveKit room; an in-process agent worker joins the same room and becomes the
persona. The package's HTTP surface is a single endpoint —
**`POST /v1/voice/token`** — that mints a short-lived LiveKit AccessToken after
auth, ownership, and credit pre-flights.

Like the rest of the stack, it carries an **edition** stance (`PERSONA_EDITION`):

- **cloud** — the token endpoint verifies the Clerk JWT (today's deployed
  behavior), scopes DB access by RLS, and meters credits.
- **community** — no-auth local voice: a fixed local owner, no JWT, unmetered,
  single-owner ownership.

## Features

- **V1 — WebRTC transport.** LiveKit OSS substrate (`livekit>=1.1`), the
  `POST /v1/voice/token` AccessToken endpoint, a `VoiceRoom` facade (inbound
  resample to PCM16 mono 16 kHz, outbound 24 kHz publish), a `Session` state
  machine, and per-user voice-call concurrency via
  `pg_try_advisory_xact_lock`.
- **V2 — Streaming STT.** A provider-independent `StreamingSTT` protocol
  (mirroring the core `ChatBackend` adapter boundary), a Deepgram Nova-3
  backend, and a Silero VAD (ONNX-only) endpointing adapter.
- **V3 — Streaming TTS.** A provider-independent `StreamingTTS` protocol, a
  Cartesia Sonic backend, per-persona voice as a first-class identity
  attribute, and mid-utterance `cancel()` (the barge-in foundation).
  **Emotion-aware delivery (V12):** the persona's emotional stance (its N5
  feeling-tags) drives Cartesia `generation_config` expressivity so the voice
  *sounds* its feeling — bounded by character and restrained, fail-soft to a
  flat read, with a `PERSONA_TTS_EMOTION_ENABLED` Beta kill-switch.
- **V4 — Turn-taking + barge-in.** A four-state conversational machine
  (Listening / UserSpeaking / Processing / PersonaSpeaking), automatic
  endpointing, fast-and-discriminating interruption, a cancel watchdog, and
  full-loop latency attribution — pure-Python decision logic on the
  V1/V2/V3 seams.
- **V5 — Persona / runtime / memory integration.** Fills V4's reply-producer
  seam with real persona-conditioned, tier-routed, streaming, cancellable
  generation, and writes voice turns to the **same** episodic store as text
  (unified memory) — plus a voice latency-routing gate, off-critical-path
  history compaction, conversational voice tools, and barge-over-honest memory.
- **V10 — Capability parity.** A call does what a chat can: the persona invokes
  tools mid-call and **produces artifacts that render on screen** in the same
  `FileRendererPanel` chat uses. Tools partition by measured latency — search /
  diagram run **inline**; `generate_image` runs on a bounded **async production
  lane**, decoupled from the audio turn, so the artifact **renders the instant
  it's ready** and the persona's "it's on screen" line is **floor-gated** (a new
  agent-initiated `LISTENING→PROCESSING` turn that never talks over the user).
  Rich-output rides the **same** `RunEvent` vocabulary as chat (`tool_result` +
  `activity_*`) over the data channel — no parallel format.
- **V6 — Frontend voice client (in development).** Browser-side audio plumbing
  + UI in `persona-web`; an optional dev agent launcher fires from the token
  endpoint.
- **V8 — STT cost gating.** Bill Deepgram for the user's speech, not the whole
  call. The seam adapter's tee is *split* — the Silero VAD is always fed (so
  barge-in is never starved) while the billed backend leg is gated by
  conversational state: the shipped **idle-gate** streams only the user's turn
  (closed during persona-speaking + listening idle), and a shared
  **ring-buffer-on-reopen** flushes the run-up on every gated→open transition so
  the barge-in / post-idle first word is never clipped. The actual billed audio
  is surfaced as `VoiceLog.stt_streamed_seconds`, re-basing `stt_total_cents`
  off streamed seconds (not wall-clock). ~85 % cost reduction on a listen-heavy
  call; the within-turn onset gate was measured sub-threshold and declined.

## Install / run

`persona-voice` is a `uv` workspace package. From the repo root:

```bash
uv sync                       # install the workspace
```

`persona-voice` is consumed by `persona-api`; there is no standalone CLI. The
token-issuance app boots from `persona_voice.http.app`:

```bash
uv run uvicorn persona_voice.http.app:create_app --factory --port 8001
```

You also need a running **LiveKit OSS Server** (`docker compose up -d livekit`)
and, for real STT/TTS, a Deepgram key (`PERSONA_STT_API_KEY`) and a Cartesia
key (`PERSONA_TTS_API_KEY`). For local web development, `packages/api/run-local.sh`
boots the api (`:8000`) **and** persona-voice (`:8001`) together.

### Test

```bash
uv run pytest packages/voice                 # unit (default)
uv run pytest packages/voice -m integration  # live LiveKit + Postgres
uv run pytest packages/voice -m external     # live Deepgram / Cartesia
uv run mypy packages/voice/src
uv run ruff check packages/voice
```

## Usage / key surfaces

**The token flow.** A client that wants a voice call calls
`POST /v1/voice/token` with a `persona_id` (and optional `conversation_id`):

1. **auth** — cloud verifies the Clerk JWT; community returns a fixed local
   owner with no token required.
2. **pre-flight** — RLS-scoped persona-ownership check + credit gate (both
   no-ops in community).
3. **mint** — a short-lived LiveKit AccessToken is signed with the LiveKit API
   secret, granting access to a per-session room.
4. **response** — `{ token, room_name, livekit_url }`. The client joins the
   room over WebRTC; the in-process agent joins the same room as the persona.

`GET /v1/voices` returns the provider voice catalogue (optionally filtered by
language) for the persona voice-selector, degrading to an empty list when TTS
is unconfigured.

## Architecture (brief)

```
browser ──WebRTC──▶  LiveKit OSS Server  ◀──WebRTC──  agent worker (in-process)
   ▲                                                        │
   └── POST /v1/voice/token ──▶ persona-voice ──▶ persona-runtime ──▶ persona-core
            (auth · ownership · credits · mint)     (STT → turn-taking → reply → TTS)
```

The trunk owns the LiveKit substrate, audio frame plumbing, the streaming STT
and TTS protocols + concrete backends, the session lifecycle, voice-call
concurrency, the persona-conditioned reply producer + unified-memory write, and
the additive `VoiceLog`. Per-minute billing and the V6 frontend land later.

## License

`persona-voice` is licensed under the **MIT License** — see [LICENSE](LICENSE).
It is true OSI open source: free for **any** use, **including commercial**. It
is part of the MIT-licensed Open Persona engine
(`persona-core` / `persona-runtime` / `persona-voice`); the application layer
(`persona-api` / `persona-web`) is separately licensed
PolyForm Noncommercial 1.0.0 (source-available, noncommercial).

## Links

- [Open Persona root README](../../README.md)
- [`persona-core`](../core/README.md) · [`persona-runtime`](../runtime/README.md) · [`persona-api`](../api/README.md) · [`persona-web`](../web/README.md)
- [CHANGELOG](CHANGELOG.md)
