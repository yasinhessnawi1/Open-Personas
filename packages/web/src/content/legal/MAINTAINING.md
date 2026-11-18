# Maintaining the legal documents

`privacy.ts` (the Privacy Policy, served at `/legal/privacy`) and `terms.ts` (the Terms
of Service, served at `/legal/terms`) are the source text. The pages in
`src/app/legal/*/page.tsx` only render them. The owner asked the orchestrator to write
and keep them current (D-OWNER-11-18 (4) in the private docs `DECISIONS.md`).

## The rule

Every sentence must be true of the product as it runs in production. When a change
touches one of the files below, re-read the sections that depend on it, update the text,
bump `version` (patch for wording, minor for a new fact, major for a change in what users
agree to or what we do with their data) and set `lastUpdated`.

Text in `[[OWNER: ...]]` is a fact only the owner can give. It stays visible on purpose.
Do not fill one in with a guess.

`pnpm check:copy` gates both files for dashes and banned vocabulary, the same as
`en.json`. `src/components/legal/legal-pages.test.tsx` checks the pages render and that
every link to them has a page.

## Paths

Paths are relative to the repo root. `api` is `packages/api/src/persona_api`, `core` is
`packages/core/src/persona`.

## Privacy Policy (`privacy.ts`)

| Section | Depends on |
| --- | --- |
| Who is responsible | Owner facts: entity, address, contact, DPO |
| What we collect | `api/db/models.py` (`users`, `messages`, `memory_chunks`, `graph_nodes`, `graph_node_versions`, `tasks`, `approval_*`, `turn_logs`, `audit_log`, `request_telemetry`, `wellbeing_disclosures`); `api/services/image_service.py` (EXIF strip); `packages/voice/src/persona_voice/model/transcript.py` (transcripts, no audio) |
| Legal bases | The data above; Article 9(2)(a) explicit consent (a lawyer to confirm; no consent prompt exists in the app); Article 22 (no automated decisions with legal effect) |
| AI models | `core/backends/config.py` (OpenRouter and direct providers); `core/backends/openai_compat.py` (request body: no `user`, no `provider.data_collection`); `deploy/server/env/common.env.example` (model chains, and the direct provider keys: Anthropic, DeepSeek, Groq, NVIDIA, Cloudflare, custom); D-ORCH-11-18-p (free chain on `:free` models, NIM on paid); `api/config.py` and `core/stores/embedder.py` (local embeddings); `packages/runtime/src/persona_runtime/crisis_encoder.py` |
| Voice | `packages/voice/src/persona_voice/stt/config.py`, `tts/config.py` (defaults); D-OWNER-11-18-b (1) (ElevenLabs in production, Cartesia optional); `deploy/server/docker-compose.voice.yml`, `deploy/azure-voice/REQUIREMENTS.md` (host, not live yet) |
| Who else gets your data | `deploy/server/hosts/oracle.env` (region); `packages/web/vercel.json` (region); `api/services/user_service.py`, `api/routes/clerk_webhook.py` (Clerk); `api/billing/gateway.py` (Stripe fields); `packages/connectors/` (Telegram, Discord, Slack, Postmark); `packages/web/src/lib/connectors/catalogue.ts` (which connectors are live); `core/tools/builtin/_search_providers.py`, `core/config.py` (web search); `api/app.py` (E2B); `core/imagegen/config.py` (images); `packages/web/src/components/chat/renderers/_pdf-view.tsx` (unpkg) |
| Transfers | The processor list above; each processor's DPA (owner) |
| How long we keep it | `deploy/server/bin/backup.sh` (`KEEP_DAYS`, also pre-deploy dumps); `deploy/server/docker-compose.yml` and `bin/provision.sh` (log rotation by size, journald cap); `deploy/server/Caddyfile` (no access log); `api/config.py` (`billing_retention_years`); `api/services/account_erasure.py` (`retain_until`); D-ORCH-11-18-o and its amendment (5 years, Norway) |
| Deleting your data | `api/routes/memory.py` (correct, forget, preview); `api/routes/conversations.py` (`forget_memory`); `api/services/erasure_service.py` (persona erasure); `api/services/account_erasure.py`, `api/routes/account_erase.py`, `api/routes/clerk_webhook.py` (account erasure); D-R9-284-erase (7) (re-apply after restore, not built yet) |
| Wellbeing and safety | `core/wellbeing.py` (the five categories); `core/wellbeing_policy.py`; `packages/runtime/src/persona_runtime/safety_intercept.py` (crisis message); `packages/runtime/src/persona_runtime/initiative/pipeline.py` and D-ORCH-11-18-m (initiative gate; the disclosure marker ships with PR #40, and per (7) the crisis intercept writes no marker); D-OWNER-11-18 (16) (a future "fine to bring this up" control will need new text) |
| Your rights | Export does not exist yet (handled by email within one month); `api/routes/me.py` (profile edit) |
| Cookies and storage | `packages/web/src/i18n/config.ts` (`NEXT_LOCALE`); `localStorage` and `sessionStorage` keys in `packages/web/src`; `packages/web/package.json` (no analytics) |
| Security | `packages/api/alembic/versions/` (row level security policies); `deploy/server/Caddyfile*` (TLS); `deploy/server/env/api.env.example` (`MCP_CREDENTIAL_KEY`); D-ORCH-11-18-i (secrets kept from tool processes) |
| Children | Owner fact: minimum age |

## Terms of Service (`terms.ts`)

| Section | Depends on |
| --- | --- |
| Who we are, Law and disputes | Owner facts; D-OWNER-11-18-b (3) (Norway) |
| Your account | `packages/web/src/auth/sign-up.cloud.tsx` (email, password, email code) |
| What Open Persona does | `core/initiative/` (initiative), approvals in `api/db/models.py`, connectors as above |
| What AI can get wrong | `safety_intercept.py` (crisis behaviour) |
| Your content | The no-training claim in the Privacy Policy |
| Credits, plans and payment | `core/billing/plans.py` (plans, packs, expiry, USD); `core/billing/metered.py` (no rollover); `core/billing/formula.py`; D-ORCH-11-18-h and D-ORCH-11-18-s (served model, failed attempts with tokens charged); `api/billing/autotopup.py`; `api/billing/gateway.py` (Stripe Tax, address); `billing.*` keys in `packages/web/src/i18n/messages/en.json` |
| Cancelling and refunds | `api/billing/gateway.py` (portal); `settings.deleteAccountRefund` in `en.json`; the private `docs/decisions/spec_M4.md` (packs not refundable) |
| Ending your use | `api/routes/personas.py` (persona delete); `api/routes/account_erase.py` |

## Where the links live

`src/lib/legal-routes.ts` holds both paths. The sign-up line
(`src/auth/sign-up.cloud.tsx`), the account menu (`src/components/shell/account-menu.tsx`)
and the legal page footer (`src/components/legal/legal-document.tsx`) read it.
