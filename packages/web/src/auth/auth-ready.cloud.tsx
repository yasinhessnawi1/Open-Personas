"use client";

/**
 * Branded-auth readiness guard (Spec 34, black-screen hardening) — cloud-only.
 *
 * The Core-3 signal hooks `useSignIn()` / `useSignUp()` (`@clerk/react@6.7.2`,
 * re-exported by `@clerk/nextjs`) are TYPED as always returning a non-null
 * `signIn` / `signUp` and a populated `errors` projection. At runtime that
 * guarantee does NOT hold during the window right after sign-out, when the
 * Clerk client RESETS:
 *
 *   - `@clerk/shared`'s own `NullableSignInSignal` / `NullableSignUpSignal`
 *     types document that `signIn` / `signUp` can be `null`.
 *   - While `clerk.loaded` is true the hook returns the live clerk-js signal
 *     (not the safe `StateProxy` default), which is momentarily mid-reset so
 *     `errors` (and therefore `errors.fields`) can be `undefined`.
 *
 * The branded flow components read `errors.fields` and `signIn.*` / `signUp.*`
 * directly in render. An unguarded read in that reset window throws
 * (`Cannot read properties of undefined (reading 'fields')`), React unmounts
 * the tree, and — absent an error boundary — the whole screen goes black.
 *
 * `isAuthSignalReady` is the single source of truth for "the signal is safe to
 * read". `AuthLoading` renders the calm loading state INSIDE the brand shell so
 * the guard never shows a blank canvas. Both are pure / presentational and
 * never import `@clerk/*`, so the file is cloud-scoped by name only.
 */
import { useTranslations } from "next-intl";
import type { BrandCopy } from "./auth-shell.cloud";
import { AuthShell, authStyles as s } from "./auth-shell.cloud";

/**
 * The minimal not-yet-ready shape a Core-3 signal hook can expose during the
 * post-logout client reset: the resource handle and/or the `errors` projection
 * can be absent even though the published type says otherwise.
 */
export interface MaybeReadySignal {
  /** `signIn` (sign-in / reset) or `signUp` (sign-up) resource handle. */
  readonly resource: unknown;
  /** The `errors` projection; `errors.fields` is read in render. */
  readonly errors: { readonly fields?: unknown } | null | undefined;
}

/**
 * True when the signal is safe to read in render: both the resource handle and
 * the `errors.fields` projection exist. Returns `false` during the reset window
 * so callers render {@link AuthLoading} instead of dereferencing `undefined`.
 */
export function isAuthSignalReady({
  resource,
  errors,
}: MaybeReadySignal): boolean {
  return (
    resource !== null &&
    resource !== undefined &&
    errors !== null &&
    errors !== undefined &&
    errors.fields !== null &&
    errors.fields !== undefined
  );
}

/**
 * The calm loading state shown inside the brand shell while the Clerk client is
 * (re)initialising — e.g. during the post-logout reset. Mirrors the busy-button
 * spinner so it reads as "working", never as a crash. `aria-busy` + a polite
 * live region announce the state to assistive tech.
 */
export function AuthLoading({ brand }: { brand: BrandCopy }) {
  const t = useTranslations("auth.loading");
  return (
    <AuthShell brand={brand}>
      <div className={s.head}>
        <h1>{t("title")}</h1>
        <p>{t("body")}</p>
      </div>
      {/* `<output>` carries an implicit `role="status"` + polite live region,
          so assistive tech announces the loading state without an explicit
          role attribute (a11y lint prefers the semantic element). */}
      <output className={s.ready} aria-busy="true">
        <span className={s.spinner} aria-hidden="true" />
      </output>
    </AuthShell>
  );
}
