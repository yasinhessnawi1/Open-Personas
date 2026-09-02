# Open Persona Design Language

> The design-language source of truth for `persona-web`. Short, load-bearing.
> If you are about to add a colour, a font size, a shadow, a motion, or any
> persona-touching surface, read this first.
>
> Version: F1 (Phase 2). Authoritative through F2's component-system build.

---

## The north star

**Persona is an editorial instrument.**

Two words doing two jobs. *Editorial*: it has a point of view, a voice, warmth, the feel of considered prose on good paper; it is not a sterile dashboard. The personas have identity, and the surface that holds them should feel like it was authored, not generated. *Instrument*: it is precise, legible, and responsive; it shows you what it is doing (which tier handled a turn, what tool it called, what code it ran); it rewards attention; it is a tool a serious person uses to do real work, not a toy.

Every design decision is judged against holding that tension: **warm but precise, authored but legible, characterful but never cute**. Too far toward *editorial* → precious, slow, over-styled. Too far toward *instrument* → another cold AI dashboard.

The reference compositions at `/reference/*` (a dev-only route group) demonstrate the language working: `chat`, `personas`, `author`, `run`, `empty`, `chat-dark`, `swatches`. They are the artefact a contributor looks at to understand "what does this product feel like."

---

## How to read the token system

**Single source of truth:** [`src/app/globals.css`](./src/app/globals.css).
**Token model:** Tailwind v4 CSS-first. Two layers:
- `:root { ... }` and `.dark { ... }` declare the raw values (OKLCH colours, the radius scale, the font-family CSS variables).
- `@theme inline { ... }` re-exports them with `--color-*` / `--text-*` / `--radius-*` / etc. so Tailwind generates utilities (`bg-primary`, `text-tier-frontier`, etc.).

**Rules** (criterion #12, token-as-truth):
- **Never hard-code a design value in a component.** No inline hex (`#c33`), no inline OKLCH outside the token layer, no magic numbers in Tailwind arbitrary values (`text-[#c33]`, `border-[12px]`) except the rare documented cases below.
- **Add a token before using it.** If the value you need doesn't exist, add it to the appropriate selector first, then use it.
- **Additive-only, never rename.** See D-F1-7 below.

The smell test: if removing a `var(--…)` reference would silently break visual fidelity somewhere unexpected, the token is doing its job. If you find yourself reaching for an arbitrary value (`bg-[#…]`), stop and add a token instead.

---

## D-F1-7: The token-name translation table (load-bearing)

The spec prose and the scaffold tokens use **different names for the same thing**. They are NOT in conflict; they are the result of the scaffold inheriting shadcn's naming convention while the spec is written in design vocabulary.

| Spec prose says | Code token is | What it actually is |
|---|---|---|
| "the accent" / "the brand vermilion" | `--primary` | The warm vermilion at OKLCH hue 30. The product's signature accent colour. Same as `--tier-frontier` (intentional semantic overload: frontier IS the heavyweight tier AND the brand). |
| "warm surface tint" / "subtle paper-tint" | `--accent` | A low-chroma neutral at OKLCH hue 66 (light) / hue 60 (dark). The hover-state surface and shadcn-primitive resting-state tint. NOT the brand colour. |
| "ink" / "body text colour" | `--foreground` | Warm dark at OKLCH hue 55. The primary text colour. |
| "paper" / "background" | `--background` | Warm cream at OKLCH hue 75 (light) / warm dark at hue 60 (dark). The base canvas. |

**Do not rename either `--primary` or `--accent`.** Both names are consumed by:
- 14 shadcn `base-nova` primitives in [`src/components/ui/`](./src/components/ui/), generated against the standard names.
- Clerk's sign-in surface via `@clerk/ui/themes/shadcn` (the `appearance={{ theme: shadcn }}` wiring in [`src/app/layout.tsx`](./src/app/layout.tsx)).
- Every existing screen in `(app)` and `(auth)` route groups.

A rename would ripple breakage; the translation table is the cheap defence.

---

## Colour system (light + dark, OKLCH)

All colour tokens declared in `:root` (light) and `.dark` (dark). The token swap is the ONLY light/dark mechanism; `next-themes` toggles `class="dark"` on `<html>`; no per-component `dark:` redefinitions.

### Semantic core

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--background` | `oklch(0.985 0.006 75)` | `oklch(0.19 0.008 60)` | base canvas (warm cream / warm deep) |
| `--foreground` | `oklch(0.24 0.012 55)` | `oklch(0.94 0.008 75)` | body text |
| `--card` | `oklch(0.997 0.004 75)` | `oklch(0.225 0.009 60)` | raised surfaces |
| `--primary` | `oklch(0.585 0.196 30)` | `oklch(0.66 0.19 33)` | brand vermilion / primary actions / focus rings |
| `--primary-foreground` | `oklch(0.995 0.008 80)` | `oklch(0.2 0.02 40)` | text on primary |
| `--secondary` | `oklch(0.95 0.012 72)` | `oklch(0.27 0.01 60)` | user message bubble / secondary buttons |
| `--accent` | `oklch(0.94 0.02 66)` | `oklch(0.3 0.014 60)` | hover surface tint (NOT vermilion, see D-F1-7) |
| `--muted` | `oklch(0.955 0.01 72)` | `oklch(0.27 0.01 60)` | skeleton, inline-code bg |
| `--border` | `oklch(0.9 0.012 72)` | `oklch(0.92 0.015 75 / 10%)` | decorative dividers |
| `--destructive` | `oklch(0.577 0.245 27.325)` | `oklch(0.704 0.191 22.216)` | error states |

### Tier temperature scale (cool → hot)

The routing-tier signal, formalised in T03 + tuned in T14.

| Token | Light | Dark | Reads as |
|---|---|---|---|
| `--tier-small` | `oklch(0.6 0.045 232)` | `oklch(0.66 0.05 232)` | slate-blue, low chroma: cool, dependable |
| `--tier-mid` | `oklch(0.6 0.135 70)` | `oklch(0.74 0.13 75)` | warm amber, mid chroma: warming |
| `--tier-frontier` | `oklch(0.585 0.196 30)` | `oklch(0.66 0.19 33)` | vermilion, high chroma: hot, == `--primary` |

**Chroma carries the signal.** Lightness is non-monotonic (small=0.6, mid=0.6, frontier=0.585 in light mode); chroma escalates (0.045 → 0.135 → 0.196). The cool→hot reading is the chroma × hue combination, not lightness.

**`--tier-frontier == --primary`.** The brand vermilion doubles as the frontier-tier signal. Documented as deliberate; do not split.

**T14 history:** the `--tier-mid` light value was darkened from `oklch(0.7 0.13 73)` → `oklch(0.6 0.135 70)` during the T14 contrast pass. The original landed at 2.61:1 on the paper background, under the WCAG-AA UI 3:1 bar. The lift to L=0.6 clears 3:1 while keeping the cool→hot reading intact.

**T14 history:** the `--primary-foreground` light value was lifted from `oklch(0.99 0.01 80)` → `oklch(0.995 0.008 80)` to clear WCAG-AA 4.5:1 on the white-on-vermilion button text (was 4.48:1).

### Persona identity-colour palette (the §4 differentiator)

The curated 12-hue palette + the deterministic derivation are in [`src/lib/persona-identity.ts`](./src/lib/persona-identity.ts). They are the F1 deliverable and the load-bearing design decision for the persona-identity visual language.

- **The palette is 12 OKLCH triples** at fixed `L=0.60`, `C=0.13`, drawn from two zones of the hue wheel:
  - Greens-to-cyans: hues 90, 110, 135, 158, 180, 200.
  - Indigos-to-roses: hues 260, 280, 300, 320, 340, 355.
- **Excluded zones** (where persona colours would collide with brand or tier signaling):
  - Around `--primary` vermilion (hue 30, ±25°): hues 5 to 55.
  - Around `--tier-mid` amber (hue 73, ±15°): hues 58 to 88.
  - Around `--tier-small` slate (hue 232, ±13°): hues 219 to 245.
- **The derivation function** (`derivePersonaIdentityColor(persona)`) is pure, sync, deterministic. FNV-1a 32-bit hash of `persona.id` + a Fibonacci-hash mix (`Math.imul(h, 0x9E3779B1)`) + modulo 12. Same persona → same colour, forever. No stored state. The Fibonacci mix is what guarantees the three live demo personas (Astrid 340° rose · Kai 180° teal · Maren 135° forest-green) land on three distinct hues.
- **Override path:** when a persona has `avatar_url` set (spec-08's field), `<PersonaAvatar>` shows the image. The identity colour still derives; it remains the accent the surrounding header underline and message border-left consume.

The 12-persona swatch sheet at [`/reference/swatches`](./src/app/reference/swatches/page.tsx) is the visual artefact for verifying the language.

#### Known characteristics under colour-vision deficiency (T14)

The CVD sim in [`src/lib/contrast.ts`](./src/lib/contrast.ts) (Brettel-Viénot model, three types) is run as a Vitest regression guard. Most palette pairs stay comfortably distinguishable; the tightest pairs are:

- **Deuteranopia / protanopia:** sage-teal 158° ↔ teal 180°; rose 340° ↔ rose-coral 355°. Both compress to ~0.05 to 0.07 sRGB distance.
- **Deuteranopia:** warm chartreuse 90° ↔ leaf-green 110°, the tightest pair at 0.018 sRGB distance.
- **Tritanopia (blue-yellow):** teal 180° ↔ sky-blue 200°, which compresses to 0.027 sRGB distance.

These pairs remain distinguishable in practice because the `<PersonaAvatar>` carries the persona's initials in Fraunces alongside the colour. The structural defence: **identity = avatar + name + small accent.** Colour reinforces; it never solely identifies.

If a future palette refinement needs to widen a specific pair, the structural constraints stay: hues outside the exclusion zones, L=0.60, C=0.13, ≥15° pairwise hue-wheel distance. The hash function + palette indexing stay; only the hue values move.

---

## Typography

| Role | Face | Source |
|---|---|---|
| Display (persona names, key headings) | **Fraunces** | next/font/google, SIL OFL 1.1 |
| UI / body | **Geist** | next/font/google, SIL OFL 1.1 |
| Monospace (code, YAML, tier badges, captions) | **Geist Mono** | next/font/google, SIL OFL 1.1 |

Licences: all three are SIL Open Font License 1.1, Apache-2.0-compatible, project-clean. No swap from the scaffold defaults (D-F1-3, D-F1-4); the Geist pairing's neutrality is what lets Fraunces do the warming.

### Type scale

Defined as `--text-{role}-size` + `--text-{role}-line-height` tokens in `@theme inline`. Consumed via the `.type-{role}` utility classes in `@layer components`:

| Class | Size | Line-height | Family |
|---|---|---|---|
| `.type-display` | 2.25rem (36px) | 1.15 | Fraunces, semibold, -0.01em tracking |
| `.type-heading` | 1.5rem (24px) | 1.25 | Fraunces, semibold, -0.005em tracking |
| `.type-body` | 0.9375rem (15px) | 1.6 | Geist |
| `.type-ui` | 0.875rem (14px) | 1.45 | Geist |
| `.type-caption` | 0.65rem (10.4px) | 1.4 | Geist Mono, +0.05em tracking, uppercase |
| `.type-code` | 0.85rem (13.6px) | 1.5 | Geist Mono |

`.type-caption` resolves the 5× `text-[0.65rem]` magic-number escape-hatch surfaced in the T01 audit (tier badge, run-status badge, step-card metadata, run-view metadata, persona detail).

---

## Spacing, radius, elevation

**Spacing:** Tailwind v4 default scale. Density posture is *generous enough to breathe, tight enough to feel precise.* If you need a custom value, the existing scale almost always works. Measure twice.

**Radius:** `--radius: 0.5rem` at root, derived steps (`--radius-sm`/`-md`/`-lg`/`-xl`/`-2xl`/`-3xl`/`-4xl`) in `@theme inline`. One known cap: [`button.tsx:26`](./src/components/ui/button.tsx#L26) uses `rounded-[min(var(--radius-md),12px)]` on size-sm. Deliberate, it prevents pill-shaped small buttons on aggressive radius systems.

**Elevation:** four named tokens added in T02. `--elevation-0` (none), `--elevation-1` (subtle card lift), `--elevation-2` (popover), `--elevation-3` (modal/sheet). Consumed via `box-shadow: var(--elevation-N)`. Not yet promoted into the shadcn primitives (F2's job).

---

## Motion

Three duration tokens + two easing tokens (T02). All in `@theme inline`:

| Token | Value | Use case |
|---|---|---|
| `--motion-duration-fast` | 120ms | snaps (focus rings, dropdown opens) |
| `--motion-duration-normal` | 200ms | standard transitions (theme swap, hover) |
| `--motion-duration-slow` | 320ms | reveals (sheet slides, drawer opens) |
| `--motion-ease-standard` | `cubic-bezier(0.4, 0, 0.2, 1)` | default easing |
| `--motion-ease-emphasized` | `cubic-bezier(0.2, 0, 0, 1)` | reveal-out easing |

### Reduced-motion path (T15)

The `@media (prefers-reduced-motion: reduce)` block in `globals.css` does two things when the user's OS setting requests reduced motion:

1. Collapses the duration tokens to 0.01ms (so consumers using `var(--motion-duration-*)` go instantaneous).
2. Globally forces `animation-duration: 0.01ms`, `transition-duration: 0.01ms`, `animation-iteration-count: 1`, `scroll-behavior: auto` with `!important`, necessary to out-specificity third-party animation rules (shadcn primitives, tw-animate-css).

**Why 0.01ms, not 0:** functional code that listens for `transitionend` / `animationend` (e.g., shadcn primitives' open/close state machines) breaks if duration is hard-zeroed. 0.01ms is the standard "instant but still firing" idiom.

**What stays animated:** functional motion that updates content. The streaming-text caret in chat compositions stays as a positional indicator (no pulse); the streaming text itself updates as tokens arrive. The timeline advances. Only decorative pulses, fades, slides, and zooms are silenced.

`disableTransitionOnChange` on `<ThemeProvider>` in [`layout.tsx`](./src/app/layout.tsx) is the **theme-flicker guard**, distinct from this reduced-motion path; both live in their own scope.

---

## The persona-identity visual language (the §4 problem and its answer)

This is the design language's load-bearing decision. Every persona-touching surface follows the **D-F1-5 composite**:

1. **`<PersonaAvatar>` in identity-coloured fill.** Circular, initials in Fraunces. The avatar carries the persona's name + identity colour in one mark.
2. **1px identity-coloured underline beneath the persona name** in the identity header. Inline-block so the underline hugs the name tightly.
3. **2px identity-coloured `border-left` on persona messages** (chat surface). Message body stays `bg-card` (neutral); the persona's words remain the figure, not her colour.

Three small accents per persona, never a wash. Multi-persona views (the [`/reference/personas`](./src/app/reference/personas/page.tsx) composition) scale to N personas without fruit-salad because no surface is ever colour-tinted.

User messages keep the scaffold's right-aligned `bg-secondary` bubble, unchanged.

**Implementation pattern** (the F2 contract):
- Wrap with `style={personaIdentityStyle(persona)}` to set `--identity-h`/`-l`/`-c` as CSS custom properties.
- Reach the identity colour via `oklch(var(--identity-l) var(--identity-c) var(--identity-h))` on `borderLeftColor` / `borderBottomColor` / `background`.
- Or use `<PersonaAvatar>`, which both sets the vars on its own element AND uses the colour.

Three skill rules of thumb:
- **If you find yourself tinting a content surface (message body, list-row background) per-persona, STOP.** Use a thin accent instead.
- **If a persona is visible without an avatar or name underline, the language is missing.** Add the composite.
- **If a persona's colour out-shouts `--primary` vermilion, the L/C is wrong.** Persona identity stays at L=0.60, C=0.13; vermilion stays at C=0.196.

---

## Reference compositions

Static, fixture-fed pages under `src/app/reference/*`, proof that the design language composes. Not live data, not part of the production navigation, not auth-protected. The agent stops at the [/reference/review](./src/app/reference/) index (T16). The human signs off criterion #7.

| Path | What | Task |
|---|---|---|
| [`/reference`](./src/app/reference/page.tsx) | Index of all compositions | (the landing page) |
| [`/reference/swatches`](./src/app/reference/swatches/page.tsx) | 12-hue palette + 12-persona derivation | T05 / D-F1-1 |
| [`/reference/chat`](./src/app/reference/chat/page.tsx) | Single-persona chat (Astrid, light) | T07 / D-F1-5 |
| [`/reference/personas`](./src/app/reference/personas/page.tsx) | Multi-persona list (Astrid + Kai + Maren) | T08 / §4 |
| [`/reference/author`](./src/app/reference/author/page.tsx) | Persona-authoring moment | T09 |
| [`/reference/run`](./src/app/reference/run/page.tsx) | Agentic run unfolding | T10 |
| [`/reference/empty`](./src/app/reference/empty/page.tsx) | Empty state (UI voice) | T11 |
| [`/reference/chat-dark`](./src/app/reference/chat-dark/page.tsx) | Same chat, dark mode | T12 / D-F1-6 |

---

## WCAG contrast: light mode (T14 measured)

Programmatic verification in [`src/lib/contrast.test.ts`](./src/lib/contrast.test.ts). Pairings below all clear WCAG-AA at the indicated bar.

| Pairing | Ratio | Threshold | Pass |
|---|---|---|---|
| foreground on background | ~12.4:1 | 4.5 (normal text) | ✅ |
| foreground on card | ~12.7:1 | 4.5 | ✅ |
| foreground on accent (surface tint) | ~10.4:1 | 4.5 | ✅ |
| mutedForeground on background | ~5.0:1 | 4.5 | ✅ |
| primaryForeground on primary (vermilion) | ~4.55:1 | 4.5 | ✅ (lifted from 4.48 → 4.55 in T14) |
| tier-frontier text on background | ~4.55:1 | 3.0 (UI/non-text) | ✅ |
| tier-mid text on background | ~3.55:1 | 3.0 | ✅ (darkened from 2.61 → 3.55 in T14) |
| tier-small text on background | ~3.95:1 | 3.0 | ✅ |
| identity palette (each of 12) vs background | ≥3.0:1 | 3.0 | ✅ |
| white text on identity palette fill | ≥3.0:1 | 3.0 (UI/non-text) | ✅ (every hue) |

**Documented exception (decorative, not a UI Component):**
- `--border` on `--background` = 1.29:1. WCAG 2.2 SC 1.4.11 exempts decorative dividers; the subtle border is intentional and reads as paper-on-paper. Consumed only as divider lines, never as a UI Component edge that conveys state.

## WCAG contrast: dark mode

All pairings in the table above hold or improve in dark mode (the warm dark base + lifted text values push every ratio up). T14 asserts these explicitly.

---

## Things to do when adding a new design surface

1. **Read this file.**
2. **Look at the reference compositions** in `/reference/*` to see how the existing language treats similar surfaces.
3. **Consume tokens.** Use Tailwind utilities (`bg-card`, `text-foreground`, `border-primary`) or `var(--…)` directly. Never inline a hex or OKLCH literal.
4. **For a persona-touching surface:** wrap with `personaIdentityStyle(persona)`, use `<PersonaAvatar>` for the avatar, follow the D-F1-5 composite (avatar + underline + border-left). Never tint a content surface per-persona.
5. **For text:** use the `.type-*` utility classes; reach for an arbitrary `text-[...]` size only when the role doesn't fit one of display / heading / body / ui / caption / code.
6. **For motion:** consume the `--motion-duration-*` tokens. The reduced-motion override silences decorative animation automatically.
7. **For light/dark:** rely on the token swap. Don't write `dark:` utilities on tokens that already have a dark counterpart.
8. **For contrast:** run `pnpm test src/lib/contrast.test.ts` after any token-value change. The test catches WCAG regressions before merge.
9. **For literals:** the **no-literals gate** (`pnpm check:no-literals`) blocks inline colour literals (`text-[#hex]`, `bg-[oklch(...)]`, etc.) and typography-sizing literals (`text-[Nrem|em|px]`) in `src/components/`, `src/app/(app)/`, `src/app/reference/`, and `src/app/page.tsx`. CI runs it in the `web` job (F2 D-F2-6, T02). To deliberately add an exception (rare, only for sub-token sizing that the `.type-*` scale cannot express), edit the `ALLOWLIST` in [`scripts/no-literals.sh`](./scripts/no-literals.sh) with a comment naming the rationale + the audit/decision entry that justifies it.

### Documented literal exceptions (the allowlist)

The no-literals gate has **three documented exceptions** for sub-token sizing that the `.type-*` scale cannot express:

| File:line | Pattern | Why it's deliberate |
|---|---|---|
| [`src/components/persona/persona-avatar.tsx:45`](./src/components/persona/persona-avatar.tsx#L45) | `text-[0.6rem]` | `<PersonaAvatar size="sm">` initials at 24px, between `.type-caption` (0.65rem) and the smallest legible inline size. F1 closeout #12 documented as deliberate. |
| [`src/components/ui/markdown.tsx:71`](./src/components/ui/markdown.tsx#L71) | `text-[0.8em]` | Inline-code relative size, em-based (relative to surrounding text, not absolute rem). F1 closeout #12 documented as deliberate. |
| [`src/components/ui/button.tsx:26`](./src/components/ui/button.tsx#L26) | `text-[0.8rem]` | `<Button size="sm">` label, shadcn `base-nova` sizing system between `.type-caption` (0.65rem) and `.type-ui` (0.875rem). Surfaced in F2 T01 audit; T03 retokenise documented this as deliberate. |

Plus a **known-legacy list** of `text-[0.65rem]` occurrences that F2's T16 / T28 / T30 close to `.type-caption` during the retokenise sweep. The legacy list shrinks as those tasks land (each closure = one entry removed from the gate's LEGACY array). See [`scripts/no-literals.sh`](./scripts/no-literals.sh) for the current list.

**Positional / structural utilities are NOT design values** and are NOT in the gate's scope. `w-[3px]` (caret width), `h-[calc(100svh-3.5rem)]` (viewport calculation), `max-w-[80%]` (percentage layout), `left-[14px]` (pixel-perfect alignment), `size-[1.2rem]` (icon size between scale steps), `rounded-[min(var(--radius-md),12px)]` (deliberate-pill-prevention): all are legitimate Tailwind arbitrary-value uses. The gate's grep patterns target only `(text|bg|border|ring|fill|stroke|outline|decoration)-\[(#|oklch\(|rgb\(|hsl\()` and `text-\[\d+\.?\d*(rem|em|px)\]`.

---

## Where things live

- **The tokens:** [`src/app/globals.css`](./src/app/globals.css)
- **Persona identity palette + derivation:** [`src/lib/persona-identity.ts`](./src/lib/persona-identity.ts)
- **WCAG / CVD math:** [`src/lib/contrast.ts`](./src/lib/contrast.ts)
- **The persona avatar:** [`src/components/persona/persona-avatar.tsx`](./src/components/persona/persona-avatar.tsx)
- **Reference compositions:** [`src/app/reference/*`](./src/app/reference/)
- **shadcn primitives (retokenised in F2 T03 to T12):** [`src/components/ui/*`](./src/components/ui/)
- **The decisions log:** [`/docs/DECISIONS.md`](../../docs/DECISIONS.md) (D-F1-1..7, D-F2-1..15)
- **The spec:** [`/docs/specs/phase2/spec_F1/`](../../docs/specs/phase2/spec_F1/), [`/docs/specs/phase2/spec_F2/`](../../docs/specs/phase2/spec_F2/)

---

## Component Reference

The F2 component inventory (what exists, when to reach for it, and the F2 anti-patterns to avoid) lives in **[`COMPONENTS.md`](./COMPONENTS.md)** (the sibling reference doc).

D-F2-2 lock note: the inline form would have exceeded the ~150-line threshold (the F2 inventory is ~30 components across 9 categories), so the reference is the sibling form. Capability-UI specs (F3+) read `COMPONENTS.md` to know what's already built before adding anything new.

Per-component entries name: path, server/client tag (D-F2-3), props summary, "use when," and "don't use for" (the F2 anti-patterns: accent-not-wash, the D-F1-5 fill-via-PersonaAvatar rule, the reserved-primary discipline).

---

*End of document. Read before adding a design surface. Update when adding a load-bearing token. For "is there already a component for this?" check [`COMPONENTS.md`](./COMPONENTS.md) first.*
