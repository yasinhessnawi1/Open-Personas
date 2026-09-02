# Component Reference (F2)

> The F2 "what exists, when to reach for it" index. Capability-UI specs (F3+) read this to know what's already built before adding anything new.
>
> Companion to [`DESIGN.md`](DESIGN.md) (the token system + voice) and to the F2 spec at [`/docs/specs/phase2/spec_F2/`](../../docs/specs/phase2/spec_F2/). D-F2-2 lock: this doc is the sibling form (the inline `## Component Reference` section in DESIGN.md exceeded the ~150-line threshold).
>
> Per-component fields:
> - **Path**: where it lives in the repo.
> - **Tag**: `server` or `client` per D-F2-3 (Server Component default; client only when the component uses hooks, refs, browser APIs, or interactive event handlers).
> - **Props**: the public surface in shorthand. Optional types use `?`.
> - **Use when**: the situations the component was designed for.
> - **Don't use for**: the F2 anti-patterns (accent-not-wash; positional ≠ design value; D-F1-5 fill-via-PersonaAvatar; reserved primary).

---

## 1. UI primitives (shadcn-extending, retokenised T03 to T12)

These are the base-nova primitives. Token consumption flows through F1's `@theme inline` so `--color-*` / `--text-*` / `--radius-*` / `--motion-*` / `--elevation-*` resolve naturally; no parallel shadcn theme block.

### Button (T03)

- **Path:** [`src/components/ui/button.tsx`](src/components/ui/button.tsx)
- **Tag:** server (client when consumer passes interactive `onClick`)
- **Props:** `variant?: "default" | "destructive" | "outline" | "secondary" | "ghost" | "link"`, `size?: "default" | "sm" | "lg" | "icon"`, `asChild?: boolean`, plus HTML button props. `buttonVariants()` exported for the `<Link className={buttonVariants()}>` asChild pattern.
- **Use when:** all CTAs (Generate, Save, Send), form submissions, secondary actions in headers, icon-only triggers.
- **Don't use for:** route navigation (use `<Link>` directly); toggleable state (use a `role="switch"` button or aria-pressed pattern); the F2 primary-button vermilion is the brand cue; don't fill a wide region with it (accent-not-wash; one strong CTA per surface).

### Card (T04)

- **Path:** [`src/components/ui/card.tsx`](src/components/ui/card.tsx)
- **Tag:** server
- **Props:** `size?: "sm" | "default" | "lg"`, plus HTML div props.
- **Use when:** every elevated content container: message bodies, section cards on detail/settings pages, run timeline step cards, EmptyState/ErrorState bases.
- **Don't use for:** inline labels (use a span); button-like clickable affordances (use a button or asChild pattern); positional wrappers (use `<Stack>` or `<Section>` instead).

### Badge (T05)

- **Path:** [`src/components/ui/badge.tsx`](src/components/ui/badge.tsx)
- **Tag:** server
- **Props:** `variant?: "default" | "secondary" | "destructive" | "outline"`, plus HTML span props.
- **Use when:** terse identifiers (language tag, tool name chips, worldview epistemic markers). Always pair with `.type-caption font-mono uppercase` for the F2 caption voice.
- **Don't use for:** the run TierBadge (use the dedicated `<TierBadge>`, which knows the tier-temperature palette); long-form labels (use UI text); status indicators that need a pulse cue (use RunStatusBadge or ToolRunningIndicator).

### Avatar (T06)

- **Path:** [`src/components/ui/avatar.tsx`](src/components/ui/avatar.tsx)
- **Tag:** server
- **Props:** `<Avatar>` wrapper + `<AvatarImage>` + `<AvatarFallback>` slots (shadcn base-nova).
- **Use when:** non-persona avatars (e.g., the Clerk profile in settings header). Falls back to initials via `<AvatarFallback>`.
- **Don't use for:** persona avatars: **use `<PersonaAvatar>` instead**, which composes this primitive AND drives the per-persona identity colour (D-F1-5). Using bare `<AvatarFallback>` on a persona surface re-introduces the `bg-primary/10` uniform-fill D-F1-5 violation.

### Sheet (T07)

- **Path:** [`src/components/ui/sheet.tsx`](src/components/ui/sheet.tsx)
- **Tag:** client (`@base-ui/react` portal + state)
- **Props:** `<Sheet>` + `<SheetTrigger>` + `<SheetContent side?: "left" | "right" | "top" | "bottom">` + `<SheetHeader>` + `<SheetTitle>` + `<SheetDescription>`.
- **Use when:** mobile navigation (the AppShell mobile menu), full-screen modals on narrow viewports, side panels for editing.
- **Don't use for:** small confirmations (use Toast or inline EmptyState); content that would fit inline (a Sheet is heavy chrome).

### DropdownMenu (T08)

- **Path:** [`src/components/ui/dropdown-menu.tsx`](src/components/ui/dropdown-menu.tsx)
- **Tag:** client
- **Props:** standard base-nova `<DropdownMenu>` + `<DropdownMenuTrigger>` + `<DropdownMenuContent>` + `<DropdownMenuItem>` + `<DropdownMenuLabel>` + `<DropdownMenuSeparator>`.
- **Use when:** theme toggle (the tri-state Light/Dark/System), profile menu, contextual actions on a list row.
- **Don't use for:** primary navigation (use `<Nav>` in the shell); long lists (use a Sheet or full page).

### Tooltip (T09)

- **Path:** [`src/components/ui/tooltip.tsx`](src/components/ui/tooltip.tsx)
- **Tag:** client
- **Props:** standard base-nova `<Tooltip>` + `<TooltipTrigger>` + `<TooltipContent>`.
- **Use when:** icon-only buttons where the action isn't immediately obvious, hover-discoverable hints on dense surfaces.
- **Don't use for:** anything load-bearing: tooltips are unreliable on touch; if the user *needs* the information, surface it inline instead.

### Input (T10)

- **Path:** [`src/components/ui/input.tsx`](src/components/ui/input.tsx)
- **Tag:** server (consumer wires interactivity)
- **Props:** all HTML input props; uses `.type-ui` for the input text + retokenised border + focus ring through `--ring-*`.
- **Use when:** single-line text input: search, name fields, simple form rows.
- **Don't use for:** multi-line content (use Textarea); rich content (use a Monaco editor mount); rating/toggles (use specific widgets).

### Textarea (T10)

- **Path:** [`src/components/ui/textarea.tsx`](src/components/ui/textarea.tsx)
- **Tag:** server (consumer wires interactivity)
- **Props:** all HTML textarea props; `.type-ui` resize-vertical default. Compose `field-sizing-content` for auto-grow (used in chat composer + AuthorWizard description).
- **Use when:** chat composer, ask-user prompt answer, persona description authoring, any free-text input that benefits from auto-grow.
- **Don't use for:** YAML editing (use the dedicated YamlEditor lazy-loading Monaco); short single-line input (use Input).

### Markdown (T11)

- **Path:** [`src/components/ui/markdown.tsx`](src/components/ui/markdown.tsx)
- **Tag:** client (react-markdown 10.x with safe-by-default plugins; no raw HTML)
- **Props:** `children: string` (the raw markdown source).
- **Use when:** persona reply rendering (MessageElement consumes this live per-chunk after the 2026-06-06 amendment), run final-output card, persona detail background prose.
- **Don't use for:** trusted-html rendering (this is intentionally safe-by-default, with no `rehype-raw`); user-input echo where markdown is undesirable (just use a `<p>` with `whitespace-pre-wrap`).

### Collapsible (preserved shadcn)

- **Path:** [`src/components/ui/collapsible.tsx`](src/components/ui/collapsible.tsx)
- **Tag:** client
- **Use when:** ToolCallCard expand/collapse, FAQ-style content disclosure.
- **Don't use for:** content that should always be visible (just render it).

### ScrollArea (preserved shadcn)

- **Path:** [`src/components/ui/scroll-area.tsx`](src/components/ui/scroll-area.tsx)
- **Tag:** client
- **Use when:** scrollable content where the native scrollbar would clash (sidebar overflow, long lists in a Sheet).
- **Don't use for:** the page body (let the browser scroll natively).

### Separator (preserved shadcn)

- **Path:** [`src/components/ui/separator.tsx`](src/components/ui/separator.tsx)
- **Tag:** server
- **Use when:** horizontal rule between dropdown sections, top-level page chunks where a Card wouldn't fit.
- **Don't use for:** decorative dividers (use spacing instead, since empty space carries the same signal with less ink).

### Skeleton (preserved shadcn, superseded for F2 surfaces)

- **Path:** [`src/components/ui/skeleton.tsx`](src/components/ui/skeleton.tsx)
- **Tag:** server
- **Use when:** legacy callers only. **Prefer the T21 patterns:** `<SkeletonLine>`, `<SkeletonBlock>`, `<SkeletonAvatar>`, `<Spinner>`. They resolve animation through F1 `--motion-duration-*` tokens.

### AuthedImage (F4-promoted, D-F4-X-authedimage-f2-promotion)

- **Path:** [`src/components/ui/authed-image.tsx`](src/components/ui/authed-image.tsx)
- **Tag:** client (uses `useAuthedImageBlobUrl`)
- **Props:** `personaId: string`, `workspacePath: string`, `mediaType: string`, `alt: string`, `className?`.
- **Use when:** every render of an authed image served by Spec 13's `GET /v1/personas/:id/uploads/:ref` (Bearer-only auth path). Composes `useAuthedImageBlobUrl` + the three loading / 404 / 5xx affordances. Used by F3 message-attached images (T10) AND F4's `<InlineVisual>` (T05) + `<ImageLightbox>` (T12).
- **Don't use for:** non-authed images (use a plain `<img>` or `next/image`); user-uploaded composer previews before send (use `useObjectURL` instead: distinct lifecycle for browser `File` objects vs server-fetched bytes).
- **Origin:** landed as F3-local for D-F3-X-image-serve-auth; promoted to F2 by F4 T16 strangler-fig (the second-consumer trigger). A re-export shim at `src/components/chat/authed-image.tsx` preserves the F3 import path; removable once all in-repo callers migrate.

---

## 2. Persona-identity components (D-F1-5 composite carriers)

These are the §4 individuality enforcement points. Every persona-touching surface MUST use one of these so the identity-coloured avatar fill + name underline + border-left composite stays consistent.

### PersonaAvatar (F1 T06 + F2 T14 promotion)

- **Path:** [`src/components/persona/persona-avatar.tsx`](src/components/persona/persona-avatar.tsx)
- **Tag:** server
- **Props:** `persona: AvatarPersona` (id + name + optional avatar_url), `size?: "sm" | "md" | "lg"`, `className?`.
- **Use when:** every persona avatar: list rows, identity headers, run viewer header, chat header. Sets `--identity-l/c/h` custom properties via `personaIdentityStyle()` so descendants can read the derived hue.
- **Don't use for:** non-persona avatars (use the bare shadcn Avatar); abstract entities like tools or system messages.

### PersonaCard (T14)

- **Path:** [`src/components/persona/persona-card.tsx`](src/components/persona/persona-card.tsx)
- **Tag:** server (Link wrapping when `href` provided)
- **Props:** `persona: PersonaCardPersona` (extends AvatarPersona with `role: string`), `href?: string`, `className?`.
- **Use when:** the persona list grid, criterion-#11 evidence panels, any "summary row of personas" surface.
- **Don't use for:** persona detail header (use `<PersonaIdentityHeader>`, a heavier composite); chat message identity (use `<PersonaIdentityHeader>` for the chat-top header AND `<MessageElement>` for per-message identity).

### PersonaIdentityHeader (T13)

- **Path:** [`src/components/persona/persona-identity-header.tsx`](src/components/persona/persona-identity-header.tsx)
- **Tag:** server
- **Props:** `persona: IdentityHeaderPersona` (AvatarPersona + role + optional constraint), `showConstraints?: boolean` (D-F2-8: true on chat + detail, false on list), `size?: "sm" | "md" | "lg"`, `className?`.
- **Use when:** the chat screen header (size="md"), the persona detail header (size="lg"). The D-F1-5 composite: avatar + 1px identity-coloured underline beneath the name + role in muted UI text + optional constraint cue.
- **Don't use for:** list rows (use `<PersonaCard>`, tighter); the run viewer header (use bare `<PersonaAvatar>` + manual byline, since the run header wants the persona small with the task title dominant).

---

## 3. Layout primitives (T20)

Composed by every (app) route's content body. Centralises max-width, padding, breakpoint policy.

### PageBody

- **Path:** [`src/components/layout/index.tsx`](src/components/layout/index.tsx)
- **Tag:** server
- **Props:** `width?: "narrow" | "default" | "wide"` (default `"default"` = max-w-4xl), `className?`.
- **Use when:** the immediate child of every `(app)/*/page.tsx`. Sets max-width + responsive horizontal padding + `mx-auto` centering.
- **Don't use for:** the chat surface (uses its own `h-[calc(100svh-3.5rem)]` viewport layout, not a width-bounded body); reference compositions (have their own layout language).

### PageHeader

- **Path:** [`src/components/layout/index.tsx`](src/components/layout/index.tsx)
- **Tag:** server
- **Props:** `title: ReactNode`, `subtitle?: ReactNode`, `actions?: ReactNode`, `className?`.
- **Use when:** the top of every list/settings page. Fraunces `.type-heading` title + muted `.type-ui` subtitle + right-aligned actions slot.
- **Don't use for:** the chat header (uses `<PersonaIdentityHeader>` instead); the run viewer header (the task title carries the role of `<h1>`).

### Section

- **Path:** [`src/components/layout/index.tsx`](src/components/layout/index.tsx)
- **Tag:** server
- **Props:** `heading?: ReactNode`, `children: ReactNode`, `className?`.
- **Use when:** chunking content inside a page. Renders a `<section>` with optional Fraunces `.type-heading` h2 + flex-col children.
- **Don't use for:** the content body itself (use a Card inside the Section); cards that already have a heading (just use the Card directly).

### Stack

- **Path:** [`src/components/layout/index.tsx`](src/components/layout/index.tsx)
- **Tag:** server
- **Props:** `gap?: 2 | 3 | 4 | 5 | 6 | 8` (default 4), `className?`.
- **Use when:** vertical flex containers where you want a token-aligned gap. The default replacement for `<div className="flex flex-col gap-N">`.
- **Don't use for:** horizontal layouts (use a flex-row div); single-child containers (no gap matters, just use a wrapper if you need one).

### Grid

- **Path:** [`src/components/layout/index.tsx`](src/components/layout/index.tsx)
- **Tag:** server
- **Props:** `cols: { base?, sm?, md?, lg? }` (each 1 to 4), `gap?: 2 | 3 | 4 | 5 | 6 | 8`, `className?`.
- **Use when:** responsive multi-column layouts. The persona list uses `{ base: 1, sm: 2, lg: 3 }`; the tools+skills row uses `{ base: 1, sm: 2 }`.
- **Don't use for:** masonry / variable-height grids (use CSS grid by hand); single-column "stacks" (use `<Stack>`).

---

## 4. Pattern primitives (T21, T22, T23)

### SkeletonLine, SkeletonBlock, SkeletonAvatar, Spinner (T21)

- **Path:** [`src/components/patterns/loading.tsx`](src/components/patterns/loading.tsx)
- **Tag:** server
- **Props:**
  - `<SkeletonLine className?>`: single bar (h-3 w-full default).
  - `<SkeletonBlock lines?: number, className?>`: multi-line paragraph skeleton.
  - `<SkeletonAvatar size?: "sm" | "md" | "lg", className?>`: circle.
  - `<Spinner size?: "sm" | "md" | "lg", className?, label?>`: implicit `role="status"` via `<output>`.
- **Use when:** any `loading.tsx` Next.js boundary (every rebuilt screen has one); inline "fetching" states; AuthorLoading cycling-status presentation.
- **Don't use for:** the chat streaming text caret (use `<StreamingTextRenderer>`, which has its own measured-locked mechanism); the thinking indicator (lives inside `<StreamingTextRenderer>`, redesigned 2026-06-06 with visible italic label).

### EmptyState (T22)

- **Path:** [`src/components/patterns/empty-state.tsx`](src/components/patterns/empty-state.tsx)
- **Tag:** server
- **Props:** `icon?: ReactNode`, `title: ReactNode`, `description?: ReactNode`, `action?: ReactNode`, `className?`.
- **Use when:** a list/collection is empty in an honest way: "no personas yet," "no usage yet." F1 voice: inviting, not apologetic.
- **Don't use for:** error states (use `<ErrorState>`); transient loading (use SkeletonLine/Block); the chat empty state (the chat composer is its own surface).

### ErrorState (T22, D-F2-9 locked: one template + per-status copy)

- **Path:** [`src/components/patterns/error-state.tsx`](src/components/patterns/error-state.tsx)
- **Tag:** server
- **Props:** `status: "default" | 422 | 429 | 402`, `copy: { title, description?, detail?, action? }`, `className?`. Plus `pydantic422Detail(error)` helper for Spec-08 field-level errors.
- **Use when:** surface-level error panels: credits-exhausted (402), validation failure (422), rate-limit (429), generic 5xx (default). Tone-coloured ring matches the status.
- **Don't use for:** inline form errors (use `.type-ui text-destructive` with `role="alert"`, see AuthorWizard); transient toasts (use the toast system).

### ToastProvider + useToast + toast() (T23)

- **Path:** [`src/components/patterns/toast.tsx`](src/components/patterns/toast.tsx)
- **Tag:** client (uses sonner@2.0.7, zero transitive deps, MIT)
- **Props:** `<ToastProvider />` mount-once in AppShell; `toast.success(msg)` / `toast.error(msg)` / `toast(msg, { description })` from any client tree.
- **Use when:** transient confirmations (saved, copied, sent), non-blocking error notifications.
- **Don't use for:** errors that need a path forward (use `<ErrorState>`); long-form messages (use a modal); confirmations the user needs to act on (use a confirm dialog).

### FadeTransition + SlideTransition (T23)

- **Path:** [`src/components/patterns/transition.tsx`](src/components/patterns/transition.tsx)
- **Tag:** client
- **Props:** `<FadeTransition show, children, className?>` / `<SlideTransition show, from?: "top" | "bottom" | "left" | "right", children, className?>`.
- **Use when:** elements that mount/unmount with a coherent F1 motion duration (the transitions resolve `--motion-duration-fast/normal/slow` via Tailwind utilities; reduced-motion silences via the F1 T15 universal `!important` path).
- **Don't use for:** continuous animation (use `animate-pulse` / `animate-spin` via class); CSS-only transitions on hover (just use the `transition-colors` utility).

---

## 5. Shell (T19)

The AppShell composition. Authenticated layout for every `(app)/*` route.

### AppShell

- **Path:** [`src/components/shell/app-shell.tsx`](src/components/shell/app-shell.tsx)
- **Tag:** client (owns the desktop sidebar + mobile sheet trigger + theme/persona context wiring)
- **Props:** `children: ReactNode`.
- **Use when:** the (app) root layout: mounts the sidebar, theme provider, persona context, toast provider.
- **Don't use for:** unauthenticated routes (use the sign-in layout); reference compositions (have their own layout).

### AppSidebar / SidebarBody / Nav / MobileNav / Brand / PersonaProvider

- **Paths:** `src/components/shell/*.tsx`
- **Tag:** client (Nav + MobileNav use Link + active-route state); server (Brand + SidebarBody pure presentation)
- **Use when:** internal to AppShell only. PersonaProvider is consumed by per-route `<PersonaProvider persona>` wrappers (chat / detail / run) that advertise the current persona to the shell.
- **Don't use for:** anything outside `<AppShell>`. They assume the shell's context.

---

## 6. Theme (T24)

### ThemeProvider

- **Path:** [`src/components/theme-provider.tsx`](src/components/theme-provider.tsx)
- **Tag:** client (next-themes wrapper)
- **Use when:** mounted once in the root layout. Persists user theme choice to localStorage (D-09-10).

### ThemeToggle

- **Path:** [`src/components/theme-toggle.tsx`](src/components/theme-toggle.tsx)
- **Tag:** client
- **Use when:** the shell header. Tri-state via T08 dropdown-menu (Light / Dark / System). F2 enhancement: explicit `--motion-duration-fast` on Sun/Moon icon-swap.

---

## 7. Chat domain (T15, T16, T17 + scaffold-preserved)

### MessageElement (T15, D-F2-15 interleaved layout)

- **Path:** [`src/components/chat/message-element.tsx`](src/components/chat/message-element.tsx)
- **Tag:** client (walks the events log + manages caret/indicator state)
- **Props:** `message: MessageElementView` (includes `events?: MessageEvent[]` for the D-F2-15 interleaved path; `content + tools[]` retained for back-compat), `persona: AvatarPersona`, `prevMessage?: MessageElementView`, `className?`.
- **Use when:** every persona/user message in the chat surface. The D-F1-5 composite (avatar via PersonaAvatar + 2px identity-coloured border-left) + the D-F2-7 once-per-turn avatar rule + the D-F2-15 interleaved text/tool layout all live here.
- **Don't use for:** run timeline (use `<StepCard>`, the agent loop has different structure); the chat input (the composer is in `<ChatWindow>`).

### StreamingTextRenderer (T17, D-F2-5 mechanism B locked from X-F2-1)

- **Path:** [`src/components/chat/streaming-text-renderer.tsx`](src/components/chat/streaming-text-renderer.tsx)
- **Tag:** client
- **Props:** `text: string`, `streaming?: boolean`, `thinking?: boolean`, `thinkingLabel?: string`, `className?`.
- **Use when:** legacy stacked-layout fallback in `<MessageElement>` (when `events[]` is absent). Implements measured-locked mechanism B (useTransition + rAF-coalesced append).
- **Don't use for:** the D-F2-15 interleaved path (MessageElement now uses `<Markdown>` directly for live token-granularity markdown rendering, see message-element.tsx); plain text rendering outside chat (use `<Markdown>` or a styled `<p>`).

### TierBadge (T16)

- **Path:** [`src/components/chat/tier-badge.tsx`](src/components/chat/tier-badge.tsx)
- **Tag:** server
- **Props:** `tier: string`.
- **Use when:** below each terminal persona message, below each run step. Closes the `text-[0.65rem]` legacy via `.type-caption`.
- **Don't use for:** the run-level tier (use the inline `.type-caption` span in RunView header; it's a single tier label, not a per-message badge); the persona language tag (use a generic `<Badge>` with `.type-caption font-mono uppercase`).

### ToolCallCard (preserved)

- **Path:** [`src/components/chat/tool-call-card.tsx`](src/components/chat/tool-call-card.tsx)
- **Tag:** client (Collapsible)
- **Use when:** rendering a tool invocation + result. Consumed by both `<MessageElement>` (interleaved at stream position) and `<StepCard>` (vertical list in agentic run).

### ChatWindow (T26-updated)

- **Path:** [`src/components/chat/chat-window.tsx`](src/components/chat/chat-window.tsx)
- **Tag:** client (owns the textarea, send, scroll-to-bottom)
- **Props:** `conversationId: string`, `persona: AvatarPersona`, `tier?: string`, plus initial messages.
- **Use when:** the (app)/chat/[id] page. Consumes `useChat` for the SSE plumbing.

---

## 8. Runs domain (T30)

All five files were retokenised (every `text-[0.65rem]` → `.type-caption`, `text-sm` → `.type-body`/`.type-ui`). The `useRun` hook + `runViewFromEvents` consumption are preserved verbatim.

### RunView

- **Path:** [`src/components/runs/run-view.tsx`](src/components/runs/run-view.tsx)
- **Tag:** client (owns cancel + the streaming timeline)
- **Props:** `runId: string`, `initial: RunStatusResponse`.
- **Use when:** the (app)/runs/[runId] page body.

### RunTimeline

- **Path:** [`src/components/runs/run-timeline.tsx`](src/components/runs/run-timeline.tsx)
- **Tag:** client
- **Props:** `view: RunView` (from `src/lib/run.ts`), `onAnswer: (answer: string) => Promise<void>`.
- **Use when:** internal to RunView. Vertical ol of StepCards + left-rail connector + working tail indicator.

### StepCard

- **Path:** [`src/components/runs/step-card.tsx`](src/components/runs/step-card.tsx)
- **Tag:** client
- **Props:** `step: RunStep`, `awaiting: boolean`, `onAnswer: (answer: string) => Promise<void>`.
- **Use when:** per-step card in the run timeline.

### RunStatusBadge

- **Path:** [`src/components/runs/run-status-badge.tsx`](src/components/runs/run-status-badge.tsx)
- **Tag:** server
- **Props:** `status: RunStatus` (running / completed / cancelled / max_steps_reached / error).
- **Use when:** internal to RunView. Status reads as temperature: live=vermilion pulse, done=cool, faults=warm.

### AskUserPrompt

- **Path:** [`src/components/runs/ask-user-prompt.tsx`](src/components/runs/ask-user-prompt.tsx)
- **Tag:** client
- **Props:** `question: string`, `onAnswer: (answer: string) => Promise<void>`.
- **Use when:** inside a StepCard when the agentic loop blocks on a question.

---

## 9. Personas + Settings domain (T29, T31)

### AuthorWizard (T29)

- **Path:** [`src/components/personas/author-wizard.tsx`](src/components/personas/author-wizard.tsx)
- **Tag:** client
- **Props:** `tools: string[]`, `skills: string[]`.
- **Use when:** the (app)/personas/new page body. Three phases (describe / loading / review). `useAuthor` + `MAX_REFINE_ROUNDS = 3` + `<PersonaEditor>` composed verbatim.

### PersonaEditor (preserved scaffold, D-09-9 form ⇄ Monaco sync)

- **Path:** [`src/components/personas/persona-editor.tsx`](src/components/personas/persona-editor.tsx)
- **Tag:** client (owns the form state + Monaco lazy-load)
- **Use when:** new persona authoring (AuthorWizard composes it) + persona edit page (`/personas/[id]/edit`).
- **Don't rewrite**: D-09-9 single-source-of-truth (the parsed PersonaDoc) governs form ↔ YAML.

### PersonaForm (preserved scaffold)

- **Path:** [`src/components/personas/persona-form.tsx`](src/components/personas/persona-form.tsx)
- **Tag:** client
- **Use when:** internal to PersonaEditor. Renders the structured form for identity / constraints / self-facts / worldview / tools / skills.

### YamlEditor (preserved scaffold)

- **Path:** [`src/components/personas/yaml-editor.tsx`](src/components/personas/yaml-editor.tsx)
- **Tag:** client (Monaco lazy-load via `next/dynamic` ssr:false, D-09-8)
- **Use when:** PersonaEditor's "Raw YAML" toggle.

### StartRunForm (preserved scaffold)

- **Path:** [`src/components/personas/start-run-form.tsx`](src/components/personas/start-run-form.tsx)
- **Tag:** client
- **Use when:** the "Give Astrid a task" CTA on the persona detail page.

### PreferencesCard (T31-retokenised)

- **Path:** [`src/components/settings/preferences-card.tsx`](src/components/settings/preferences-card.tsx)
- **Tag:** client (next-themes + useBoolSetting + LOCALE_COOKIE)
- **Use when:** the (app)/settings page. Theme tri-state + tier-badge switch + language toggle. The `left-[1.125rem]` switch-thumb is a positional pixel (audit-noted), not a design value.

---

## F5 promotions (Spec F5 T07, D-F5-X-document-primitives-f2-promotion)

The two F3-local document-chip components were promoted to canonical F2 home via the strangler-fig pattern. **11th additive-precedent chain entry.**

### DocumentChip (promoted F5 T07)

- **Path:** [`src/components/ui/document-chip.tsx`](src/components/ui/document-chip.tsx) (canonical F2 home)
- **Strangler-fig shim:** `src/components/chat/composer/document-chip.tsx` re-exports `{ DocumentChip, type DocumentChipProps }` from `@/components/ui/document-chip`. Existing F3 / chat-composer imports continue to work; new consumers (F5 artifact view T15) import from the F2 path directly.
- **Tag:** client (next-intl + lucide-react)
- **Use when:** rendering a document attachment chip in the chat composer (F3) or the F5 artifact view (T15) doc rows.
- **Don't use for:** image previews (use F2 `<AuthedImage>` + composer `<ComposerImagePreview>`); generic file cards (compose `<Card>` directly).

### ConversationDocumentList (promoted F5 T07)

- **Path:** [`src/components/ui/conversation-document-list.tsx`](src/components/ui/conversation-document-list.tsx) (canonical F2 home)
- **Strangler-fig shim:** `src/components/chat/composer/conversation-document-list.tsx` re-exports `{ ConversationDocumentList, type ConversationDocumentListProps }` from `@/components/ui/conversation-document-list`.
- **Tag:** client
- **Use when:** rendering the per-conversation document chip strip in the F3 composer or the F5 conversation-scoped document column (T15).

## What was deleted in F2

The scaffold's `src/components/personas/persona-card.tsx` (30 LOC, scaffold version with the `bg-primary/10 text-primary` uniform-fill D-F1-5 violation) was removed at T32 close, orphaned after T27 swapped the list page to the F2 `<PersonaCard>` at `src/components/persona/persona-card.tsx` (singular path).

The scaffold's `src/components/chat/message-bubble.tsx` + its orphan test were removed in T26 close, replaced by `<MessageElement>` (which absorbed the streaming caret + bubble + tool-card composition).

---

## See also

- [`DESIGN.md`](DESIGN.md): the F1 token system + voice + how-to-add-a-surface checklist.
- [`/docs/specs/phase2/spec_F2/`](../../docs/specs/phase2/spec_F2/): the F2 spec + decisions (D-F2-1..15) + tasks (T01 to T34) + audit + measurements.
- [`/docs/DECISIONS.md`](../../docs/DECISIONS.md): the cross-spec one-liner log.

---

*End of reference. When you add a new F2 component, append it here with the same shape: name, path, tag, props, use when, don't use for. When you remove or rename, update both this file AND the consumers.*
