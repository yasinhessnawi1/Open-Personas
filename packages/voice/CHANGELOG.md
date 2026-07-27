# Changelog, persona-voice

All notable changes to `persona-voice` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-spec entries are added by the close-out phase of each spec. The authoritative
project-wide changelog lives at [`/CHANGELOG.md`](../../CHANGELOG.md); this file
mirrors only the `persona-voice`-touching surface.

---

## [Unreleased]

(empty, future post-V2 work lands here)

---

## [0.V2.0]: 2026-06-10

> **Spec V2, Streaming Speech-to-Text, Phase 6 complete.** Provider-independent `StreamingSTT` Protocol mirroring Spec 02 `ChatBackend` verbatim + concrete Deepgram Nova-3 backend + Silero VAD ONNX-only adapter + V1 `STTStream` seam adapter + the ONE V1 source delta (T07 StreamingLoop additivity at **21 LOC**; ≤50 architectural-bet budget cleared with 60 % margin) + VoiceLog 4 additive STT fields + content-hash-only audit + PERSONA_STT_* env block + integration spine (criterion #2 BINARY proven) + external smoke with 4 measurement gates (operator-pass disposition). **2876 default tests passing + 6 V2 integration + 4 V2 external (skipped without API key); 0 regressions; +135 from V1 close baseline.** Wall-clock onset framing honest per D-V2-2 LOCK: 85-90 ms TYPICAL / 116-121 ms WORST-CASE including `SileroFramer` reframer.

### Added

- **`src/persona_voice/stt/` subpackage**: 10 new files; ~1300 LOC including Google-style docstrings. `__init__` / `types` (boundary records) / `errors` (STT domain exception hierarchy) / `protocol` (`StreamingSTT` + `SpeechActivityListener` Protocols; `StreamingSTT.transcripts()` returns `AsyncIterator` per D-02-5; `SpeechActivityListener` kept OFF `StreamingSTT` per Pipecat issue #1323 production-bug precedent + D-V2-X-activity-listener-shape) / `config` (`StreamingSTTConfig(BaseSettings, env_prefix="PERSONA_STT_")` with `SecretStr` api_key + Deepgram endpointing/utterance-end + Silero tuning Field constraints) / `_factory` (`load_streaming_stt` dispatcher mirroring `load_backend` shape) / `deepgram_backend` (concrete D-V2-1 LOCK launch backend; fail-fast `STTAuthenticationError` at construction per Spec 02 D-02-10; lazy WebSocket open on first `push_audio`; full 401/403/429/400/disconnect error matrix) / `vad_silero` (D-V2-X-silero-implementation-shape 3 pillars: ONNX-only path / `SileroFramer` mandatory / lazy-construct + explicit `load()` prewarm; `session_state_provider` ctor arg for D-V2-X-echo-cancellation-v1-dependency TTS-mute-window mitigation; `benchmark_onset_latency` records baseline wall-clock onset INCLUDING `SileroFramer` overhead) / `seam_adapter` (`V1STTStreamSeamAdapter` composes V2 backend + Silero VAD into V1 `STTStream`-Protocol-shaped object; tees `push_audio`; merges activity events with provider-as-corroborator stamping via Pydantic v2 `model_copy(update={"corroborates": True})`) / `audit` (Spec 15 D-15-X-hard-line-filter content-hash-only mirror; `STT_AUDIT_HASH_ALG="sha256"`).
- **V1 source delta (T07 architectural bet) at [`src/persona_voice/loop/streaming.py:198-217`](src/persona_voice/loop/streaming.py#L198-L217): 21 added lines.** Additive `speech_activity: SpeechActivityListener | None = None` ctor param + private storage + `@property speech_activity` getter/setter for production composition wiring. **VALIDATES** the additive-port shape over the composite-Protocol alternative; V1's 12 existing `streaming_loop` tests pass byte-for-byte. **Additive-precedent chain entry #24** (D-V2-X-streaming-loop-additivity-shape).
- **VoiceLog 4 additive STT fields (T08)** at [`src/persona_voice/logging.py`](src/persona_voice/logging.py): D-V2-X-cost-discipline + D-05-9 + D-V1-X-first-token-measurement-coordination. `stt_partial_first_at: datetime | None` + `stt_audio_pushed_at: datetime | None` + `stt_provider_cost_cents_per_minute: float | None` (**Deepgram streaming PAYG $0.0048/min = 0.48 cents/min** per Phase-3 critic correction; $0.0042/min on Growth; the $0.0077/min figure cited in earlier drafts was for **pre-recorded** transcription, NOT streaming) + `stt_total_cents: float | None`. VoiceLog stays frozen Pydantic v2 + `extra="forbid"`; V1's existing 18 VoiceLog tests pass byte-for-byte.
- **104 V2 unit tests** at `tests/unit/stt/` + **6 V2 integration tests** at [`tests/integration/test_v2_streaming_stt.py`](tests/integration/test_v2_streaming_stt.py) (criterion #2 BINARY proof + #3 + #5 + #6 + #9 structural negative assertion + D-V2-X-echo-cancellation-v1-dependency mitigation) + **4 V2 external smoke tests** at [`tests/external/test_real_provider_smoke.py`](tests/external/test_real_provider_smoke.py) marked `@pytest.mark.external` + `pytest.mark.skipif(PERSONA_STT_API_KEY is None)` (Gate #1 Deepgram TTFT-to-first-FINAL ≤250 ms P50 / ≤400 ms P95; Gate #2 Arabic WER ≤25 % on MSA + EG + Gulf + Levant; Gate #3 Silero wall-clock onset ≤150 ms P95 incl. `SileroFramer`; Gate #4 Silero FP rate ≤30 % on TTS-bleedthrough corpus). Falsification routes per `docs/specs/phase2/spec_V2/decisions.md`.
- **`deepgram-sdk>=4.0,<5`** added to `pyproject.toml` deps (MIT-licensed + PEP 561 typed; transitive stack permissive: httpx BSD-3 / websockets BSD-3 / aiohttp Apache-2.0 / pydantic MIT / dataclasses-json MIT). **`silero-vad-lite>=0.2,<1`** added (MIT; bundles `silero_vad.onnx` v5 + C++ ONNX runtime wrapper; NO torch transitive, avoids 200-500 MB). Root `pyproject.toml` adds `[[tool.mypy.overrides]]` blocks for `deepgram*` + `silero_vad_lite*` mirroring V1 `livekit.*` pattern.
- **PERSONA_STT_* env block** at root `.env.example`: 11 vars; ~66 lines; mirrors PERSONA_IMAGEGEN_* commented-out-with-comments discipline. Operator hint carries Phase-3-critic-corrected pricing + Feb 2026 PAYG concurrency cap tripling to 150 streams + D-V1-5 per-user advisory-lock context + per-language quality-routing fallback to Speechmatics behind the Protocol seam.

---

## [0.1.0]: 2026-06-07

> **First release of `persona-voice`: the voice trunk.** A 4th uv workspace package + LiveKit OSS substrate + WebRTC transport facade + session lifecycle + streaming-loop skeleton with V2/V3/V4/V5 Protocol seams + advisory-lock per-user concurrency + VoiceLog instrumentation. Branch (A) per D-V1-1 (R-V1-1 ruled out aiortc on documented 17-20× latency overhead). **84 voice unit + 5 voice integration tests against live LiveKit Server + Postgres + 10 persona-core auth tests from the T03 extraction.** Binary criterion #3 (full-duplex) **structurally proven** via live LiveKit Server.

### Added (Spec V1, Real-Time Voice Service and WebRTC Transport, Phase 6 complete)

- **`packages/voice/` as 4th uv workspace member** with `persona-core[postgres]` + `livekit>=1.1,<2` + `livekit-api>=1.1,<2` deps (both Apache-2.0, D-V1-X-livekit-sdk-license-stack confirmed via PyPI). Root `pyproject.toml` extended with mypy_path + pytest testpaths + `livekit.*` mypy-override + per-file ruff ignore. Root `conftest.py` adds `packages/voice/src` for the editable-`.pth` iCloud-hidden-flag workaround.
- **Token-issuance endpoint** at [`src/persona_voice/http/app.py`](src/persona_voice/http/app.py): `POST /v1/voice/token` (JWT-authed via the extracted persona-core verifier); checks persona ownership via the configured DB; mints a LiveKit `AccessToken` (room=`persona:<session_id>`; identity=user_id; metadata={persona_id, conversation_id, session_id}; TTL 10min default); returns `{token, room_name, livekit_url}`. RLS-shape 404 on cross-tenant.
- **`VoiceRoom` facade over `livekit.rtc.Room`** at [`src/persona_voice/transport/room.py`](src/persona_voice/transport/room.py): connect / disconnect / `track_subscribed` → `InboundAudioFrame` drain (resampled to canonical PCM16 mono 16 kHz per D-V1-6) / `publish_outbound` (PCM16 mono 24 kHz) / `capture_outbound_frame` / `RoomSubstrate: Protocol` for test substrate injection. `build_voice_room()` is the production constructor.
- **Session lifecycle state machine** at [`src/persona_voice/session/state_machine.py`](src/persona_voice/session/state_machine.py): `Session` frozen Pydantic v2 + `SessionState = Literal["created","active","ended"]` + `SessionLifecycleEvent` StrEnum (7 V4-aligned values) + `SessionEventListener` Protocol + `InvalidSessionStateError`. `make_session_rls_engine(url, user_id)` is the per-session RLS engine (D-V1-X-rls-engine-shape, pool_size=1, user_id baked into checkout listener). `attach_to_room(voice_room)` wires `Room.on('disconnected')` → `end()` → engine.dispose → advisory-lock release via tx rollback.
- **Streaming-loop skeleton with V2/V3/V4/V5 Protocol seams** at [`src/persona_voice/loop/streaming.py`](src/persona_voice/loop/streaming.py): `STTStream` (V2 push) / `TTSStream` (V3 + cancel for V4 barge-in) / `ModelReplyProducer` (V5) / `PassThroughEchoMode` StrEnum (ECHO/DISABLED). V2→V5→V3 pipeline runs as `asyncio` Task; D-V1-6 sample-rate guard raises on mismatch.
- **Per-user voice-call concurrency** at [`src/persona_voice/concurrency.py`](src/persona_voice/concurrency.py): `acquire_voice_call_concurrency(*, conn, user_id)` mirrors `imagegen/concurrency.py` verbatim per D-V1-X-d15x-precedent-binding. `pg_try_advisory_xact_lock(('x' || md5(:user_id))::bit(64)::bigint)` auto-releases on tx commit/rollback; multi-worker-correct from day one. `VoiceConcurrencyCappedError(PersonaError)` analogue maps to 429 + Retry-After at the endpoint integration site.
- **VoiceLog instrumentation** at [`src/persona_voice/logging.py`](src/persona_voice/logging.py): frozen Pydantic v2 + `extra="forbid"` per D-05-9. LiveKit canonical hops (`eou_at` / `stt_final_at` / `llm_first_token_at` / `tts_first_byte_at` / `audio_first_play_at`) coordinated with Spec 18 D-18-X-first-token-measurement-impl per D-V1-X-first-token-measurement-coordination. V1's binding share (`transport_in_ms` / `transport_out_ms` / `loop_overhead_ms`; 100ms P50 / 150ms P95 CI gate). `JSONLVoiceLogWriter` durable per-write flush.
- **T08 binary criterion #3 PROVEN** at [`tests/integration/test_full_duplex.py`](tests/integration/test_full_duplex.py): agent (persona-voice's `VoiceRoom`) + client (raw `rtc.Room`) join the same LiveKit Server Room; publish/subscribe a 2s sine tone in BOTH directions concurrently; both ends receive ≥10 frames at the canonical D-V1-6 rates. Full-duplex is structurally proven; V4 barge-in foundation is real, not aspirational.

### Added (Spec 19 amendment, chain entry 19)

- **L6b (chain 19) D-19-X-voice-token-credit-gate**: `POST /v1/voice/token` is gated by `credits_service.require_credits` per D-11-12 pattern: returns HTTP 402 + structured `credits_exhausted` body BEFORE the LiveKit `AccessToken` is minted. Closed-spec additive extension; no Spec 11 / Spec V1 reopen. Per-minute voice deduction lands post-v0.1 once the V5 stack proves call-length telemetry.

### Cross-spec coordination (Spec V1)

- **Spec 08 additive amendment (9th in chain)**: `make_jwt_verifier` + `AuthenticatedUser` extracted to `persona.auth.jwt_verifier`. persona-api re-exports; no test breakage. Per D-12-X / D-16-X precedent.
- **Spec 15 D-15-X-concurrency-cap precedent binding**: `acquire_voice_call_concurrency` is the verbatim mirror of `imagegen/concurrency.py`. The kickoff's "Postgres rate-limit table" generic lean was wrong; corrected at Phase 1 and locked at Phase 4.
- **Spec 18 D-18-X-first-token-measurement-impl coordination**: VoiceLog's `llm_first_token_at` field uses the same shape Spec 18 records at `runtime/loop.py:465-468`. One measurement convention, two producers, V5 reads from both.
- **Spec 11 D-11-1/2/3 hosting amendment**: `livekit-server` Go binary becomes a sidecar container in docker-compose; v0.1 single-VPS sizing reviewed at MAINTENANCE.md.
- **Architecture §10 voice OOS supersession**: line 769 "Voice. Out of scope for September." superseded by a new §11-equivalent voice-layer block. Additive precedent chain: 10th entry per D-V1-X-architecture-md-update.

### Decisions (Spec V1)

23 decisions locked at Phase 4 per [`docs/specs/phase2/spec_V1/decisions.md`](../../docs/specs/phase2/spec_V1/decisions.md). Headline: **D-V1-1 branch (A): self-hosted LiveKit OSS Server + `livekit` low-level Python SDK + hand-implemented Protocols**. Rejected aiortc on R-V1-1's documented evidence (Issue #775 500-600ms LAN Opus latency 17-20× over the 30ms budget; Issue #505 SRTP blocks asyncio event loop; unfixed memory leaks); rejected language change because it defeats in-process persona-core access. Cloudflare Realtime TURN primary + Twilio NTS fallback (D-V1-2; $0 + $5/mo at v0.1 scale). All 23 mirrored to [`docs/DECISIONS.md`](../../docs/DECISIONS.md).
