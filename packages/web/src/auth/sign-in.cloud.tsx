"use client";

/**
 * `SignIn` — cloud (Clerk) branded sign-in (Spec 34, Cluster B).
 *
 * A custom email + password sign-in flow built on Clerk's Core-3 signal hook
 * `useSignIn()` (`@clerk/react@6`, re-exported by `@clerk/nextjs`). The hook
 * returns `{ signIn, errors, fetchStatus }`; the flow drives the `SignInFuture`
 * resource:
 *
 *   1. start (email)     → signIn.create({ identifier: email })
 *   2. password          → signIn.password({ password })
 *      - status 'complete'             → finalize
 *      - status 'needs_second_factor'  → MFA email-code step (3a/3b)
 *   3a. mfa send          → signIn.mfa.sendEmailCode()      (emails a 6-digit code)
 *   3b. mfa verify        → signIn.mfa.verifyEmailCode({ code })  (status -> 'complete')
 *   4. finalize          → signIn.finalize({ navigate })   (sets the active session)
 *
 * The email-code second factor reuses the sign-up flow's exact code-entry UX
 * (per-digit `OtpInput` that auto-submits when full + a throttled "Resend code"
 * cooldown), so MFA looks identical to the verification step users already know.
 *
 * OAuth (gated OFF for v1 via OAUTH_PROVIDERS) is wired through
 * signIn.sso({ strategy, redirectUrl, redirectCallbackUrl }). Errors are read
 * from the returned `{ error }` and the hook's `errors` projection, then mapped
 * to themed copy; `fetchStatus === 'fetching'` drives the loading state.
 *
 * Verified against the installed Core-3 types and Clerk's custom-flow docs.
 * Hook-driven branches need the user's real-browser pass; the pure logic
 * (error mapping, OAuth gate) is unit-tested separately.
 */
import { useSignIn } from "@clerk/nextjs";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import {
  ErrorAlert,
  Field,
  HiddenUsernameField,
  OAuthRow,
  OtpInput,
  PasswordInput,
  useResendCooldown,
} from "./auth-fields.cloud";
import {
  type ClerkErrorLike,
  clerkErrorToMessage,
  dedupeFieldError,
  formatCooldown,
} from "./auth-flow.cloud";
import { ArrowIcon, MailIcon } from "./auth-icons.cloud";
import { AuthLoading, isAuthSignalReady } from "./auth-ready.cloud";
import { signInRedirectTarget } from "./auth-redirect.cloud";
import { AuthShell, authStyles as s } from "./auth-shell.cloud";
import { useInFlightGuard } from "./use-in-flight-guard.cloud";
import { useSignedInRedirect } from "./use-signed-in-redirect.cloud";

/** The steps of the email→password (→ email-code second factor) sign-in flow. */
type Step = "start" | "password" | "mfa";

export function SignIn() {
  const t = useTranslations("auth.signIn");
  const ta = useTranslations("auth");
  const tf = useTranslations("auth.fields");
  const tBrand = useTranslations("auth.brand");
  const tError = useTranslations("auth.error");
  // The brand panel takes plain strings; build them from the catalogue.
  const signInBrand = {
    kicker: tBrand("signIn.kicker"),
    tagline: tBrand("signIn.tagline"),
    note: tBrand("signIn.note"),
    compact: tBrand("signIn.compact"),
  };
  const mfaBrand = {
    kicker: tBrand("mfa.kicker"),
    tagline: tBrand("mfa.tagline"),
    note: tBrand("mfa.note"),
    compact: tBrand("mfa.compact"),
  };
  const { signIn, errors, fetchStatus } = useSignIn();
  const router = useRouter();
  // Redirect an already-signed-in visitor to the app instead of rendering a form
  // that would 400 with `session_exists` ("You're already signed in.") on submit.
  const redirectTarget = signInRedirectTarget();
  const { redirecting } = useSignedInRedirect(redirectTarget);

  const [step, setStep] = useState<Step>("start");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const cooldown = useResendCooldown();
  // Single-flight latch: a fast double-Enter / double-click can fire a submit
  // handler twice before `busy`/`fetchStatus` flips, sending a duplicate request
  // (the second hits an "already in progress"/"already complete" error). It also
  // closes the window where the MFA OTP `onComplete` auto-submit and the form
  // `onSubmit` both target the verify in one code entry. Guard each async step so
  // it runs once per user action.
  const { runGuarded } = useInFlightGuard();

  // An active session was detected — show the calm loading state while the
  // redirect to the app commits, never the sign-in form.
  if (redirecting) {
    return <AuthLoading brand={signInBrand} />;
  }

  // Guard the post-logout reset window: the typed-non-null `signIn` / `errors`
  // can both be absent while the Clerk client re-initialises. Reading
  // `errors.fields` (or `signIn.*`) before then throws in render and — without
  // an error boundary — blanks the whole screen. Show the calm loading state
  // inside the brand shell instead until the signal is safe to read.
  if (!isAuthSignalReady({ resource: signIn, errors })) {
    return <AuthLoading brand={signInBrand} />;
  }

  const busy = fetchStatus === "fetching";
  const fieldErrors = errors.fields;
  // Dedupe against the top banner so Clerk errors surfaced at both the global
  // and field level (e.g. "Couldn't find your account.") never render twice.
  const emailError = dedupeFieldError(
    fieldErrors.identifier?.message,
    formError,
  );
  const passwordError = dedupeFieldError(
    fieldErrors.password?.message,
    formError,
  );

  /** Navigate after a completed sign-in, honouring any pending session task. */
  const finishSession: Parameters<typeof signIn.finalize>[0] = {
    navigate: ({ session, decorateUrl }) => {
      if (session?.currentTask) return;
      // Land on the configured app target (NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_
      // REDIRECT_URL → /personas), not the bare "/" the flow used before.
      const url = decorateUrl(redirectTarget);
      if (url.startsWith("http")) window.location.href = url;
      else router.push(url);
    },
  };

  /** Step 1: bind the identifier so the password step has context + a userData chip. */
  const handleEmail = async (event: React.FormEvent) => {
    event.preventDefault();
    await runGuarded(async () => {
      setFormError(null);
      const { error } = await signIn.create({ identifier: email.trim() });
      if (error) {
        setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
        return;
      }
      setStep("password");
    });
  };

  /**
   * Step 2: submit the password, then branch on the resulting status.
   *
   * - `complete`             → finalize (set the active session).
   * - `needs_second_factor`  → the account has email-code MFA enabled; send the
   *   code and move to the OTP step. If the send fails we stay on the password
   *   step with a themed error rather than stranding the user on an empty form.
   * - anything else          → surface a calm prompt rather than silently stall.
   */
  const handlePassword = async (event: React.FormEvent) => {
    event.preventDefault();
    await runGuarded(async () => {
      setFormError(null);
      const { error } = await signIn.password({ password });
      const handled =
        signIn.status === "complete" || signIn.status === "needs_second_factor";
      if (error && !handled) {
        setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
        return;
      }
      if (signIn.status === "complete") {
        await signIn.finalize(finishSession);
        return;
      }
      if (signIn.status === "needs_second_factor") {
        const { error: sendError } = await signIn.mfa.sendEmailCode();
        if (sendError) {
          setFormError(
            clerkErrorToMessage(sendError as ClerkErrorLike, tError),
          );
          return;
        }
        cooldown.start();
        setCode("");
        setStep("mfa");
        return;
      }
      // needs_client_trust / needs_new_password etc. are not part of the v1
      // email+password config; surface a calm prompt rather than silently stall.
      setFormError(clerkErrorToMessage(null, tError));
    });
  };

  /**
   * Step 3: verify the email-code second factor, then finalize on completion.
   *
   * Guarded single-flight (shared `runGuarded`): the OTP auto-submit and the form
   * submit can both fire for one code entry; without the latch the second call
   * hits Clerk's "already verified" → 400. Defense-in-depth: a wrong code returns
   * an error and the status stays `needs_second_factor`, so we surface the themed
   * error and DO NOT finalize — a bad code can never sign the user in.
   */
  const submitMfaCode = (value: string) =>
    runGuarded(async () => {
      setFormError(null);
      const { error } = await signIn.mfa.verifyEmailCode({ code: value });
      const alreadyComplete = signIn.status === "complete";
      if (error && !alreadyComplete) {
        setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
        return;
      }
      if (signIn.status === "complete") {
        await signIn.finalize(finishSession);
      } else {
        setFormError(clerkErrorToMessage(null, tError));
      }
    });

  const handleMfaVerify = async (event: React.FormEvent) => {
    event.preventDefault();
    await submitMfaCode(code);
  };

  /** Resend the email second-factor code (throttled by the cooldown). */
  const resendMfa = async () => {
    if (cooldown.isCoolingDown || busy) return;
    setFormError(null);
    const { error } = await signIn.mfa.sendEmailCode();
    if (error) {
      setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
      return;
    }
    cooldown.start();
  };

  /** Reset to the email step so the user can correct the identifier. */
  const changeIdentifier = async () => {
    await signIn.reset();
    setPassword("");
    setCode("");
    setFormError(null);
    setStep("start");
  };

  /** OAuth (only reachable when OAUTH_PROVIDERS is non-empty). */
  const handleOAuth = async (strategy: string) => {
    setFormError(null);
    // `strategy` originates from the controlled OAUTH_PROVIDERS list; narrow to
    // the SDK's exact sso() strategy param type at the boundary.
    type SsoStrategy = Parameters<typeof signIn.sso>[0]["strategy"];
    const { error } = await signIn.sso({
      strategy: strategy as SsoStrategy,
      redirectUrl: "/sign-in/sso-callback",
      redirectCallbackUrl: "/",
    });
    if (error)
      setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
  };

  const startForgot = () => router.push("/reset-password");

  // Email-code second factor (only reached when the account has MFA enabled).
  // Reuses the sign-up verification UX exactly: per-digit `OtpInput` that
  // auto-submits when full, plus a throttled "Resend code" cooldown.
  if (step === "mfa") {
    const cooldownLabel = formatCooldown(cooldown.remaining);
    return (
      <AuthShell brand={mfaBrand}>
        <div className={s.head}>
          <h1>{t("mfaTitle")}</h1>
          <p>
            {t.rich("mfaBody", {
              email: signIn.identifier ?? email,
              em: (chunks) => (
                <strong className={s.resendStrong}>{chunks}</strong>
              ),
            })}
          </p>
        </div>
        <form
          className={s.body}
          onSubmit={handleMfaVerify}
          aria-busy={busy}
          noValidate
        >
          <ErrorAlert message={formError} />
          <OtpInput
            value={code}
            onChange={setCode}
            onComplete={submitMfaCode}
            invalid={Boolean(fieldErrors.code)}
            disabled={busy}
          />
          <div className={s.actions}>
            <button
              className={`${s.btn} ${s.btnPrimary}`}
              type="submit"
              disabled={busy || code.length < 6}
              aria-disabled={busy || code.length < 6}
            >
              {busy ? (
                <>
                  <span className={s.spinner} aria-hidden="true" />
                  {t("verifying")}
                </>
              ) : (
                <>
                  {t("verify")}
                  <ArrowIcon />
                </>
              )}
            </button>
          </div>
          {cooldown.isCoolingDown ? (
            <p className={s.resend}>
              {ta.rich("resendIn", {
                time: cooldownLabel,
                em: (chunks) => (
                  <strong className={s.resendStrong}>{chunks}</strong>
                ),
              })}
            </p>
          ) : (
            <p className={s.resend}>
              <MailIcon />
              {ta("didntGet")}{" "}
              <button
                type="button"
                className={s.link}
                onClick={resendMfa}
                disabled={busy}
              >
                {ta("resend")}
              </button>
            </p>
          )}
        </form>
        <p className={s.foot}>
          {t("wrongAccount")}{" "}
          <button
            type="button"
            className={s.link}
            onClick={changeIdentifier}
            disabled={busy}
          >
            {t("startOver")}
          </button>
        </p>
      </AuthShell>
    );
  }

  return (
    <AuthShell brand={signInBrand}>
      <div className={s.head}>
        <h1>{t("title")}</h1>
        <p>{step === "start" ? t("startSubtitle") : t("passwordSubtitle")}</p>
      </div>

      {step === "start" ? (
        <form
          className={s.body}
          onSubmit={handleEmail}
          aria-busy={busy}
          noValidate
        >
          <ErrorAlert message={formError} />
          <OAuthRow onSelect={handleOAuth} disabled={busy} />
          <Field id="si-email" label={tf("email")} error={emailError}>
            <div className={s.control}>
              <input
                className={s.input}
                id="si-email"
                name="email"
                type="email"
                autoComplete="email"
                inputMode="email"
                placeholder={tf("emailPlaceholder")}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                disabled={busy}
                required
              />
            </div>
          </Field>
          <div className={s.actions}>
            <button
              className={`${s.btn} ${s.btnPrimary}`}
              type="submit"
              disabled={busy}
              aria-disabled={busy}
            >
              {busy ? (
                <>
                  <span className={s.spinner} aria-hidden="true" />
                  {t("continuing")}
                </>
              ) : (
                <>
                  {t("continue")}
                  <ArrowIcon />
                </>
              )}
            </button>
          </div>
        </form>
      ) : (
        <form
          className={s.body}
          onSubmit={handlePassword}
          aria-busy={busy}
          noValidate
        >
          <ErrorAlert message={formError} />
          {/* Off-screen-but-in-DOM email so this password step is a complete
              credential form: password managers can associate the saved login
              and the "password forms should have a username field" a11y warning
              clears. Bound to the identifier captured in step 1. */}
          <HiddenUsernameField value={signIn.identifier ?? email} />
          <div className={s.idchip}>
            <span className={s.who}>{signIn.identifier ?? email}</span>
            <button
              type="button"
              className={s.link}
              onClick={changeIdentifier}
              aria-disabled={busy}
              disabled={busy}
            >
              {t("change")}
            </button>
          </div>
          <Field
            id="si-pw"
            label={tf("password")}
            error={passwordError}
            rowExtra={
              <button
                type="button"
                className={s.link}
                onClick={startForgot}
                disabled={busy}
              >
                {t("forgot")}
              </button>
            }
          >
            <PasswordInput
              id="si-pw"
              value={password}
              onChange={setPassword}
              autoComplete="current-password"
              placeholder={t("passwordPlaceholder")}
              invalid={Boolean(passwordError)}
              disabled={busy}
            />
          </Field>
          <div className={s.actions}>
            <button
              className={`${s.btn} ${s.btnPrimary}`}
              type="submit"
              disabled={busy}
              aria-disabled={busy}
            >
              {busy ? (
                <>
                  <span className={s.spinner} aria-hidden="true" />
                  {t("submitting")}
                </>
              ) : (
                <>
                  {t("submit")}
                  <ArrowIcon />
                </>
              )}
            </button>
          </div>
        </form>
      )}

      <p className={s.foot}>
        {t("newHere")}{" "}
        <a className={s.link} href="/sign-up">
          {t("createAccount")}
        </a>
      </p>
    </AuthShell>
  );
}
