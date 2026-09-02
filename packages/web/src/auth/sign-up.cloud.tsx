"use client";

/**
 * `SignUp` — cloud (Clerk) branded sign-up (Spec 34, Cluster C).
 *
 * A custom email + password sign-up with email-code verification, built on
 * Clerk's Core-3 signal hook `useSignUp()` (`@clerk/react@6`, re-exported by
 * `@clerk/nextjs`). The hook returns `{ signUp, errors, fetchStatus }`; the flow
 * drives the `SignUpFuture` resource:
 *
 *   1. start    → signUp.password({ emailAddress, password })
 *                 then signUp.verifications.sendEmailCode()
 *   2. verify   → signUp.verifications.verifyEmailCode({ code })
 *                 (status -> 'complete')
 *   3. finalize → signUp.finalize({ navigate })   (sets the active session)
 *
 * The 6-digit code uses the per-digit OTP input which auto-submits when full.
 * "Resend code" is throttled by a themed cooldown countdown. OAuth (gated OFF
 * for v1) is wired via signUp.sso(). Errors map to themed copy; `fetchStatus`
 * drives the loading state.
 *
 * Verified against the installed Core-3 types and Clerk's custom-flow docs.
 * Hook-driven branches need the user's real-browser pass; the pure logic
 * (cooldown formatting, error mapping, OAuth gate) is unit-tested separately.
 */
import { useSignUp } from "@clerk/nextjs";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import {
  ErrorAlert,
  Field,
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
import { signUpRedirectTarget } from "./auth-redirect.cloud";
import { AuthShell, authStyles as s } from "./auth-shell.cloud";
import { useInFlightGuard } from "./use-in-flight-guard.cloud";
import { useSignedInRedirect } from "./use-signed-in-redirect.cloud";

/** The two steps of the sign-up flow. */
type Step = "start" | "verify";

export function SignUp() {
  const t = useTranslations("auth.signUp");
  const ta = useTranslations("auth");
  const tf = useTranslations("auth.fields");
  const tBrand = useTranslations("auth.brand");
  const tError = useTranslations("auth.error");
  // The brand panel takes plain strings; build them from the catalogue.
  const signUpBrand = {
    kicker: tBrand("signUp.kicker"),
    tagline: tBrand("signUp.tagline"),
    note: tBrand("signUp.note"),
    compact: tBrand("signUp.compact"),
  };
  const verifyBrand = {
    kicker: tBrand("verify.kicker"),
    tagline: tBrand("verify.tagline"),
    note: tBrand("verify.note"),
    compact: tBrand("verify.compact"),
  };
  const { signUp, errors, fetchStatus } = useSignUp();
  const router = useRouter();
  // Redirect an already-signed-in visitor to the app instead of rendering a form
  // that would 400 with `session_exists` ("You're already signed in.") on submit.
  const redirectTarget = signUpRedirectTarget();
  const { redirecting } = useSignedInRedirect(redirectTarget);

  const [step, setStep] = useState<Step>("start");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const cooldown = useResendCooldown();
  // Single-flight latch: the OTP `onComplete` and the form `onSubmit` both target
  // `submitCode`, and `busy`/`fetchStatus` flips too late to gate the second
  // call. The ref closes that window so the code is verified exactly once.
  const { runGuarded } = useInFlightGuard();

  // An active session was detected — show the calm loading state while the
  // redirect to the app commits, never the sign-up form.
  if (redirecting) {
    return <AuthLoading brand={signUpBrand} />;
  }

  // Guard the post-logout reset window: `signUp` / `errors` can both be absent
  // while the Clerk client re-initialises (despite the typed non-null shape).
  // Reading `errors.fields` (or `signUp.*`) before then throws in render and —
  // without an error boundary — blanks the whole screen. Show the calm loading
  // state inside the brand shell until the signal is safe to read.
  if (!isAuthSignalReady({ resource: signUp, errors })) {
    return <AuthLoading brand={signUpBrand} />;
  }

  const busy = fetchStatus === "fetching";
  const fieldErrors = errors.fields;
  // Dedupe against the top banner so an error surfaced at both the global and
  // field level never renders twice (banner kept; under-field copy suppressed).
  const emailError = dedupeFieldError(
    fieldErrors.emailAddress?.message,
    formError,
  );
  const passwordError = dedupeFieldError(
    fieldErrors.password?.message,
    formError,
  );

  /** Navigate after a completed sign-up, honouring any pending session task. */
  const finishSession: Parameters<typeof signUp.finalize>[0] = {
    navigate: ({ session, decorateUrl }) => {
      if (session?.currentTask) return;
      // Land on the configured app target (NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_
      // REDIRECT_URL → /personas), not the bare "/" the flow used before.
      const url = decorateUrl(redirectTarget);
      if (url.startsWith("http")) window.location.href = url;
      else router.push(url);
    },
  };

  /** Step 1: create the account with email + password, then send the code. */
  const handleStart = async (event: React.FormEvent) => {
    event.preventDefault();
    await runGuarded(async () => {
      setFormError(null);
      const { error } = await signUp.password({
        emailAddress: email.trim(),
        password,
      });
      if (error) {
        setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
        return;
      }
      const { error: sendError } = await signUp.verifications.sendEmailCode();
      if (sendError) {
        setFormError(clerkErrorToMessage(sendError as ClerkErrorLike, tError));
        return;
      }
      cooldown.start();
      setStep("verify");
    });
  };

  /**
   * Step 2: verify the 6-digit code, then finalize on completion.
   *
   * Guarded single-flight: the OTP auto-submit and the form submit can both fire
   * for one code entry; without the latch the second call hits Clerk's
   * "already verified" → 400. Defense-in-depth: if the verify call returns an
   * error BUT the resource is already verified/complete (the first, winning call
   * landed it), treat that as success and proceed to finalize rather than
   * surfacing the spurious error.
   */
  const submitCode = (value: string) =>
    runGuarded(async () => {
      setFormError(null);
      const { error } = await signUp.verifications.verifyEmailCode({
        code: value,
      });
      const alreadyComplete = signUp.status === "complete";
      if (error && !alreadyComplete) {
        setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
        return;
      }
      if (signUp.status === "complete") {
        await signUp.finalize(finishSession);
      } else {
        setFormError(clerkErrorToMessage(null, tError));
      }
    });

  const handleVerify = async (event: React.FormEvent) => {
    event.preventDefault();
    await submitCode(code);
  };

  /** Resend the email code (throttled by the cooldown). */
  const resend = async () => {
    if (cooldown.isCoolingDown || busy) return;
    setFormError(null);
    const { error } = await signUp.verifications.sendEmailCode();
    if (error) {
      setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
      return;
    }
    cooldown.start();
  };

  /** OAuth (only reachable when OAUTH_PROVIDERS is non-empty). */
  const handleOAuth = async (strategy: string) => {
    setFormError(null);
    type SsoStrategy = Parameters<typeof signUp.sso>[0]["strategy"];
    const { error } = await signUp.sso({
      strategy: strategy as SsoStrategy,
      redirectUrl: "/sign-up/sso-callback",
      redirectCallbackUrl: "/",
    });
    if (error)
      setFormError(clerkErrorToMessage(error as ClerkErrorLike, tError));
  };

  if (step === "start") {
    return (
      <AuthShell brand={signUpBrand}>
        <div className={s.head}>
          <h1>{t("title")}</h1>
          <p>{t("subtitle")}</p>
        </div>
        <form
          className={s.body}
          onSubmit={handleStart}
          aria-busy={busy}
          noValidate
        >
          <ErrorAlert message={formError} />
          <OAuthRow onSelect={handleOAuth} disabled={busy} />
          <Field id="su-email" label={tf("email")} error={emailError}>
            <div className={s.control}>
              <input
                className={s.input}
                id="su-email"
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
          <Field
            id="su-pw"
            label={tf("password")}
            hint={tf("passwordHint")}
            error={passwordError}
          >
            <PasswordInput
              id="su-pw"
              value={password}
              onChange={setPassword}
              autoComplete="new-password"
              placeholder={t("passwordPlaceholder")}
              invalid={Boolean(passwordError)}
              describedBy="su-pw-hint"
              disabled={busy}
            />
          </Field>
          {/* Clerk Smart CAPTCHA mounts here for the custom sign-up flow (bot
              protection). Without this element Clerk warns and silently falls
              back to Invisible CAPTCHA. */}
          <div id="clerk-captcha" />
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
          <p className={s.legal}>
            {t.rich("legal", {
              terms: (chunks) => <a href="/legal/terms">{chunks}</a>,
              privacy: (chunks) => <a href="/legal/privacy">{chunks}</a>,
            })}
          </p>
        </form>
        <p className={s.foot}>
          {t("haveAccount")}{" "}
          <a className={s.link} href="/sign-in">
            {t("signIn")}
          </a>
        </p>
      </AuthShell>
    );
  }

  const cooldownLabel = formatCooldown(cooldown.remaining);

  return (
    <AuthShell brand={verifyBrand}>
      <div className={s.head}>
        <h1>{t("verifyTitle")}</h1>
        <p>
          {t.rich("verifyBody", {
            email: signUp.emailAddress ?? email,
            em: (chunks) => (
              <strong className={s.resendStrong}>{chunks}</strong>
            ),
          })}
        </p>
      </div>
      <form
        className={s.body}
        onSubmit={handleVerify}
        aria-busy={busy}
        noValidate
      >
        <ErrorAlert message={formError} />
        <OtpInput
          value={code}
          onChange={setCode}
          onComplete={submitCode}
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
                {t("verifyEmail")}
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
              onClick={resend}
              disabled={busy}
            >
              {ta("resend")}
            </button>
          </p>
        )}
      </form>
      <p className={s.foot}>
        {t("wrongAddress")}{" "}
        <a className={s.link} href="/sign-up">
          {t("goBack")}
        </a>
      </p>
    </AuthShell>
  );
}
