/**
 * Branded-auth flow helpers (Spec 34) — cloud-only, framework-free logic.
 *
 * The pure, hook-independent pieces of the custom Clerk flows live here so they
 * can be unit-tested without rendering a Clerk-bound component:
 *   - the OAuth provider gate (D-34-3: built but shipped OFF for v1),
 *   - the Clerk-error → themed-message mapper (so error surfacing is testable),
 *   - the resend-cooldown constant.
 *
 * Verified against the installed Core-3 surface (`@clerk/react@6.7.2` /
 * `@clerk/shared@4.14.0`): the signal hooks return a `{ error }` whose `error`
 * is a `ClerkError` with `code` / `message` / `longMessage`, plus an
 * `errors.fields.<field>` / `errors.global` projection. `isClerkAPIResponseError`
 * (re-exported from `@clerk/nextjs`) narrows a thrown error to one carrying an
 * `errors: ClerkAPIError[]` array. For the codes we have written copy for, that
 * copy is what the user reads; for anything else we surface `longMessage`, then
 * `message`, then a themed fallback. A raw stack is never shown.
 */

/**
 * A Clerk OAuth strategy id (`oauth_<provider>`). Declared locally as a string
 * template rather than imported, because `@clerk/nextjs` does not re-export the
 * `OAuthStrategy` type and a deep `@clerk/shared` import would be brittle; this
 * is assignment-compatible with Clerk's `signIn.sso({ strategy })` param.
 */
export type OAuthStrategyId = `oauth_${string}`;

/** A single OAuth connection shown in the "Continue with …" row. */
export interface OAuthProvider {
  /** Clerk OAuth strategy id, e.g. `"oauth_google"`. */
  readonly strategy: OAuthStrategyId;
  /** Key under `auth.oauth.*` for the button label. */
  readonly labelKey: "google" | "github";
  /** Which inline brand icon to render. */
  readonly icon: "google" | "github";
}

/**
 * OAuth providers shown on sign-in / sign-up.
 *
 * D-34-3: no OAuth provider is live in Clerk for v1, so this ships EMPTY — the
 * OAuth row renders nothing and no dead "Continue with Google" button reaches
 * users. The full provider definitions are kept (commented) so enabling later
 * is a one-line flip here PLUS enabling the connection in the Clerk Dashboard.
 *
 * To enable, replace the empty array with `OAUTH_PROVIDERS_ALL` (or a subset).
 */
export const OAUTH_PROVIDERS: readonly OAuthProvider[] = [];

/**
 * The complete provider set the design covers — Google + GitHub. NOT wired into
 * the UI by default (see `OAUTH_PROVIDERS`); referenced when OAuth is turned on.
 */
export const OAUTH_PROVIDERS_ALL: readonly OAuthProvider[] = [
  { strategy: "oauth_google", labelKey: "google", icon: "google" },
  { strategy: "oauth_github", labelKey: "github", icon: "github" },
];

/** Seconds the "Resend code" action is disabled after a send (themed countdown). */
export const RESEND_COOLDOWN_SECONDS = 30;

/**
 * The minimal shape of a Core-3 `ClerkError` we read for messaging. Matches
 * `@clerk/shared`'s `ClerkError` (code / message / longMessage). Kept structural
 * so both the hook `{ error }` return and a thrown `ClerkAPIError` element fit.
 */
export interface ClerkErrorLike {
  readonly code?: string;
  readonly message?: string;
  readonly longMessage?: string;
}

/** The `auth.error.*` keys this module can ask the catalogue for. */
export type AuthErrorKey =
  | "generic"
  | "lockout"
  | "wrongPassword"
  | "socialAccount"
  | "noAccount"
  | "breachedPassword";

/**
 * Resolves `auth.error.<key>`. Injected so this module stays pure and the copy
 * lives in the message catalogue, not here.
 */
export type AuthErrorTranslator = (key: AuthErrorKey) => string;

/**
 * Error codes that mean the account is rate-limited / temporarily locked.
 * Clerk emits these on too many failed attempts; we show a calmer, themed copy.
 */
const LOCKOUT_CODES = new Set<string>([
  "too_many_requests",
  "user_locked",
  "account_locked",
]);

/**
 * The Clerk rejection reasons a person can actually act on (R9-135).
 *
 * Clerk answers a failed sign-in with a 422 whose `errors[0].code` says exactly
 * what went wrong, and we used to flatten all of them into Clerk's own English
 * or the generic sentence, so "wrong password", "that email signs in with
 * Google" and "no account here at all" read identically. Each of these four
 * needs a different next move from the user, so each gets its own sentence.
 * Anything not listed keeps the old behaviour: Clerk's `longMessage`, then
 * `message`, then the generic line.
 */
const COPY_KEY_BY_CODE: Record<string, AuthErrorKey> = {
  form_password_incorrect: "wrongPassword",
  strategy_for_user_invalid: "socialAccount",
  form_identifier_not_found: "noAccount",
  // Clerk refuses a breached password on sign-in when HIBP is enforced, and on
  // sign-up / reset when one is chosen. It used to borrow the lockout copy,
  // which told the user to wait for something that was never going to change.
  form_password_pwned: "breachedPassword",
};

/**
 * Map a Core-3 Clerk error to a single themed message safe to show a user.
 *
 * Order: a code we have written copy for wins; then lockout/rate-limit codes
 * get the calm lockout copy; otherwise prefer the localizable `longMessage`,
 * then `message`, then the generic fallback. A raw provider stack is never
 * surfaced.
 */
export function clerkErrorToMessage(
  error: ClerkErrorLike | null | undefined,
  t: AuthErrorTranslator,
): string {
  if (!error) return t("generic");
  if (error.code) {
    const copyKey = COPY_KEY_BY_CODE[error.code];
    if (copyKey) return t(copyKey);
    if (LOCKOUT_CODES.has(error.code)) return t("lockout");
  }
  const text = error.longMessage?.trim() || error.message?.trim();
  return text && text.length > 0 ? text : t("generic");
}

/** True if the error code indicates a lockout / rate-limit condition. */
export function isLockoutError(
  error: ClerkErrorLike | null | undefined,
): boolean {
  return Boolean(error?.code && LOCKOUT_CODES.has(error.code));
}

/**
 * Deduplicate a field-level error against the top banner.
 *
 * Clerk surfaces some errors at BOTH the form level (our `ErrorAlert` banner,
 * fed by the mapped action error) AND the field level
 * (`errors.fields.<field>`), so the same text — e.g. "Couldn't find your
 * account." — would render twice (once in the banner, once under the field).
 * Keep the banner; suppress the under-field copy when it is the same message.
 *
 * Returns the field message to render under the field, or `null` to render
 * nothing there. Comparison is whitespace-insensitive so a trailing-space
 * variant still dedupes. A genuinely field-specific error (one the banner does
 * NOT show — e.g. a password-rules error with an empty banner) is preserved.
 */
export function dedupeFieldError(
  fieldMessage: string | null | undefined,
  bannerMessage: string | null | undefined,
): string | null {
  const field = fieldMessage?.trim();
  if (!field) return null;
  const banner = bannerMessage?.trim();
  if (banner && banner === field) return null;
  return fieldMessage ?? null;
}

/**
 * Format the resend cooldown for display, e.g. `formatCooldown(29) === "29s"`.
 * Returns an empty string at or below zero (cooldown elapsed → no countdown).
 */
export function formatCooldown(secondsRemaining: number): string {
  if (!Number.isFinite(secondsRemaining) || secondsRemaining <= 0) return "";
  return `${Math.ceil(secondsRemaining)}s`;
}
