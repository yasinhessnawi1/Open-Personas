# persona-web

> The Next.js 16 app for Open Persona: persona authoring, streaming chat, agentic runs, and voice, in two editions.

`persona-web` is **layer 1** of the [Open Persona](../../README.md) stack, the
surface users actually touch. It is a thin client. All business logic lives in
[`persona-api`](../api/README.md), reached over REST and SSE through an OpenAPI
generated client, with every auth touch isolated behind a swappable `@/auth` seam.

> **Note:** this is **not** the Next.js your training data knows. The app pins
> Next.js 16 (App Router, Turbopack) with breaking changes from prior versions.
> See [`AGENTS.md`](AGENTS.md), and read `node_modules/next/dist/docs/` before
> writing code.

---

## What it is and where it fits

The browser front end: Next.js 16 (App Router), TypeScript in strict mode,
Tailwind v4, shadcn/ui, an OpenAPI generated client against `persona-api`, and
Biome, Vitest, and Playwright as the verification surface. SSE streams (chat,
agentic run timelines, authoring drafts) are consumed via `fetch` plus
`ReadableStream` with hand mirrored event shapes, because OpenAPI cannot model
server sent events. State is server state first, with no global store.

It ships in **two editions**, selected at **build time** by `PERSONA_EDITION`:

- **community** (the default). A **Clerk free** build: no sign-in wall, a fixed
  local owner, middleware is a pass-through. Pairs with the community API (SQLite
  plus Chroma, no auth, no credits) for a clone and run local self-host.
- **cloud.** The owner's commercial hosting: Clerk auth (`ClerkProvider`, sign-in
  and sign-up, `<UserButton>`), JWT templated bearer tokens to the API, and the
  credits dashboard.

The edition is a **build time module swap**. Every Clerk touch is isolated behind
an `@/auth` facade (`@/auth`, `@/auth/server`, `@/auth/provider`,
`@/auth/middleware`) with `*.cloud.*` and `*.community.*` variants that share one
`types.ts` surface. `turbopack.resolveAlias` in `next.config.ts`, keyed on
`PERSONA_EDITION`, points each import at the edition's variant, so a community
build never pulls `@clerk/*` into the bundle. CI gates and
`no-restricted-imports` enforce it.

## Features

- **Persona authoring.** One sentence brief, a frontier draft streamed live (SSE
  progressive preview), a structured form that swaps to a lazy Monaco YAML editor
  and back, then save. Plus edit and a files manager.
- **Streaming chat.** SSE chat with a visible identity header, collapsible tool
  call cards, per turn tier badges, file and image attachments, and a right panel
  artifact renderer covering 10 formats (markdown, code, PDF, image, CSV, JSON,
  HTML, Mermaid, Graphviz, plaintext, with a rendered and raw toggle). The turn is
  a **persistent, resumable session**: it keeps running server side when you
  navigate away or reload, and reattaches on return (seed then tail, then
  reconcile), with a "working" indicator on the conversation row and a global
  return-to-it bar. Message actions: copy, retry, read aloud in the persona's
  voice, and **turn into file**, which extracts a message's substance into a real
  PDF, XLSX, or Markdown file in the persona workspace.
- **Agentic run viewer.** A run timeline over SSE (catch up plus reconcile on
  drop, resuming the live tail on return), inline ask user, a Markdown final
  answer, and cancel.
- **Voice.** A browser voice client (LiveKit `livekit-client`) wired to the
  `/v1/voice/token` flow: an Identity Orb call surface, plus a **persistent call
  experience**. The call lives in an app level session above the router, so it
  survives in-app navigation; a draggable, collapsible mini call bar controls it
  from anywhere; active call indicators mark the on-call persona; one call at a
  time (end and switch); resume after reload is best effort and always prompts,
  never silent; mute and push to talk; and a post call recap lands in the chat
  thread.
- **Calendar and reminders** (`/schedule`). Agenda, week, and month views over the
  server's occurrences engine, with no client side recurrence math. **New
  reminder** creates a schedule directly (humane picker, engine preview, confirm,
  with a quiet hours warning and offer), and reschedules ride the same single edit
  door.
- **Memory map** (`/memory`). The user's shared knowledge graph as a live force
  directed map with typed links (semantic, entity, temporal, causal), semantic
  search, node detail with provenance as story, and **correct** and **delete**
  actions that re-index or remove what every persona retrieves.
- **Activity and approvals** (`/tasks`, `/approvals`). The autonomy surface: what
  ran, what it produced, per task spend caps, and parked approvals resolvable from
  the inbox or inline in chat.
- **Model picker.** Per persona preferred model from a live catalog with USD price
  tags and a curated shortlist, "use tier default" as the safety net, and a sticky
  last choice default on create.
- **Settings.** Credit balance and per turn usage, theme, tier badge visibility,
  language toggle, conversations list.
- **Connectors** (`/settings/connectors`). Link messaging platforms (Telegram,
  Discord, Slack, WhatsApp, SMS, email) behind one coherent connect, step,
  progress, confirmed flow over four mechanisms (deep link, OAuth, phone code,
  email code), with connected identity display and disconnect. The list is the
  sole completion oracle. This is a thin frontend over the api's connector front
  door.
- **Notifications and consent.** One app wide notification facade (`useNotify`)
  over the toast layer, plus a persistent bell center covering every consequential
  moment: run terminal (a background run finishing while you're elsewhere),
  persona ready, and low balance at load, alongside the common chat and CRUD
  events. Consequential events (run, persona) are **server authored into a
  durable, cross device feed** (`GET /v1/me/notifications`) that survives reload
  and syncs across devices; the bell renders that union with a client session low
  balance advisory, and rows deep link to their target. Plus one consent dialog
  (`useConfirm`) for every confirmation, so there are no native browser dialogs.
- **Responsive and internationalized.** Usable at 375px, dark mode by default,
  fully internationalized via `next-intl`.

> The web v1 production redesign is complete: an identity spine `.v-*` design
> system over F1 tokens (no literal colours), a rebuilt sidebar and ⌘K palette,
> and the three signature moments (per turn tier, typed memory recall, presence).

## Install and run

`persona-web` is a **standalone Node project**. It is **not** part of the `uv`
workspace, so `uv sync` does nothing here. Use `pnpm`.

```bash
cd packages/web
pnpm install
cp .env.example .env.local        # NEXT_PUBLIC_API_BASE_URL (+ Clerk keys for cloud)
```

Prerequisites: Node 20.9 or newer, pnpm 10.x, and a running `persona-api` for live
data.

```bash
# community (default): Clerk-free, pairs with the zero-infra community API
pnpm dev                          # http://localhost:3000 (Turbopack)
pnpm build                        # community build (omits Clerk)

# cloud: Clerk auth + credits
pnpm dev:cloud                    # PERSONA_EDITION=cloud next dev
pnpm build:cloud                  # PERSONA_EDITION=cloud next build
pnpm start                        # serve a production build
```

Full stack locally: run the API (`cd ../api && bash run-local.sh`), then
`pnpm dev`.

### Verify

```bash
pnpm typecheck                    # tsc --noEmit (strict)
pnpm lint                         # Biome (format + lint)
pnpm test                         # Vitest + React Testing Library
pnpm test:e2e                     # Playwright against a real browser + live API
pnpm check:clerk-free             # community build pulls no @clerk/* into src
pnpm gen:api                      # regenerate the OpenAPI client from openapi.json
```

A UI change is done when `typecheck`, `lint`, `build`, and `test` are clean **and**
the feature works in a running browser.

## Usage and key surfaces

App structure (App Router):

- `(app)`, the authenticated app group: `chat`, `personas` (plus `new`, `[id]`,
  edit, files), `conversations`, `calls` (voice call history into transcripts),
  `runs`, `settings`. In community the group has no auth wall.
- `(auth)`, Clerk sign-in, sign-up, and reset-password in cloud; the community
  variants redirect home.
- The landing page, public, with auth aware CTAs.

**API access.** Every backend call goes through the committed OpenAPI generated
client (`src/lib`), never a hand written `fetch`. The bearer token comes from the
`@/auth` seam: a Clerk JWT template token in cloud, `null` in community. SSE
streams (chat, runs, authoring) are read via `ReadableStream`, with the event
shapes mirrored in `src/lib`.

## Architecture (brief)

```
browser ──┐
          │  Next.js 16 (App Router, Turbopack)
          │  @/auth seam ─ build-time swap (turbopack.resolveAlias)
          │     community → no-auth stub (Clerk-free bundle)
          │     cloud     → Clerk
          ▼
   persona-api  (REST + SSE, OpenAPI)
```

The web app never links `persona-core`, `persona-runtime`, or `persona-voice`
directly. Every backend concern crosses the API boundary.

## License

`persona-web` is licensed under **PolyForm Noncommercial 1.0.0**; see
[LICENSE](LICENSE) (`package.json` declares `"SEE LICENSE IN LICENSE"`). It is
**source-available, not OSI "open source"**: you may read, modify, and
self-host it for personal, research, evaluation, educational, and other
**noncommercial** use, but **commercial use requires a separate license** from
the rights holder. The engine packages
(`persona-core` / `persona-runtime` / `persona-voice`) are separately
**MIT**-licensed and free for any use.

## Links

- [Open Persona root README](../../README.md)
- [`persona-core`](../core/README.md) · [`persona-runtime`](../runtime/README.md) · [`persona-voice`](../voice/README.md) · [`persona-api`](../api/README.md)
- [AGENTS.md](AGENTS.md), the Next.js 16 house rules · [CHANGELOG](CHANGELOG.md)
