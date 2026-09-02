"use client";

import { useTranslations } from "next-intl";
/**
 * Shared branded auth form controls (Spec 34, Cluster A) — cloud-only.
 *
 * Small presentational client components reused across the sign-in / sign-up /
 * reset flows: the gated OAuth row, a password input with a show/hide toggle,
 * the inline error alert, and the per-digit OTP input. Each is tokenized
 * through the shared CSS module and accessible (associated labels, `role`/
 * `aria-*`). Flow state + Clerk calls live in the parent flow components; these
 * just render and emit values.
 */
import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { OAUTH_PROVIDERS, RESEND_COOLDOWN_SECONDS } from "./auth-flow.cloud";
import {
  AlertIcon,
  EyeIcon,
  EyeOffIcon,
  GitHubIcon,
  GoogleIcon,
} from "./auth-icons.cloud";
import { authStyles as s } from "./auth-shell.cloud";

/**
 * A resend-code cooldown timer. Returns the seconds remaining (0 when ready)
 * and a `start()` to (re)arm the countdown after a code is sent. Decrements
 * once per second via a single interval that is cleared on unmount. The display
 * formatting lives in `formatCooldown` (pure, unit-tested); this hook only owns
 * the tick.
 */
export function useResendCooldown(seconds: number = RESEND_COOLDOWN_SECONDS) {
  const [remaining, setRemaining] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const clear = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const start = useCallback(() => {
    clear();
    setRemaining(seconds);
    timerRef.current = setInterval(() => {
      setRemaining((value) => {
        if (value <= 1) {
          clear();
          return 0;
        }
        return value - 1;
      });
    }, 1000);
  }, [seconds, clear]);

  useEffect(() => clear, [clear]);

  return { remaining, start, isCoolingDown: remaining > 0 } as const;
}

/**
 * The "Continue with …" OAuth row. Renders nothing when `OAUTH_PROVIDERS` is
 * empty (D-34-3 default), so no dead button ships. When providers are enabled,
 * each button calls `onSelect(strategy)` (the parent wires `signIn.sso` /
 * `signUp.sso`). A divider follows the row only when at least one provider is
 * shown.
 */
export function OAuthRow({
  onSelect,
  disabled,
}: {
  onSelect: (strategy: string) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("auth");
  if (OAUTH_PROVIDERS.length === 0) return null;
  return (
    <>
      <div className={s.oauth}>
        {OAUTH_PROVIDERS.map((provider) => (
          <button
            key={provider.strategy}
            type="button"
            className={`${s.btn} ${s.btnOauth}`}
            onClick={() => onSelect(provider.strategy)}
            disabled={disabled}
          >
            {provider.icon === "google" ? <GoogleIcon /> : <GitHubIcon />}
            {t(`oauth.${provider.labelKey}`)}
          </button>
        ))}
      </div>
      <div className={s.divider}>{t("or")}</div>
    </>
  );
}

/**
 * A visually-hidden-but-in-DOM username/email field for the password step.
 *
 * A standalone password input is an incomplete credential form: browsers warn
 * "Password forms should have (optionally hidden) username fields for
 * accessibility", and password managers can't associate the saved login. This
 * carries the email from the identifier step as a read-only `autoComplete=
 * "username"` field so both concerns clear. It stays in the accessibility tree
 * (not `display:none` / not `aria-hidden`) but is removed from the visual flow.
 */
export function HiddenUsernameField({ value }: { value: string }) {
  const t = useTranslations("auth.fields");
  return (
    <input
      className={s.visuallyHidden}
      type="email"
      name="username"
      autoComplete="username"
      tabIndex={-1}
      aria-label={t("email")}
      value={value}
      readOnly
    />
  );
}

/** Inline themed error banner (ARIA alert). Renders nothing when `message` is empty. */
export function ErrorAlert({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className={s.alert} role="alert">
      <AlertIcon />
      <span>{message}</span>
    </div>
  );
}

/** A labelled text field with optional inline field-error + hint. */
export function Field({
  id,
  label,
  error,
  hint,
  rowExtra,
  children,
}: {
  id: string;
  label: string;
  error?: string | null;
  hint?: ReactNode;
  /** Extra content in the label row (e.g. a "Forgot password?" link). */
  rowExtra?: ReactNode;
  children: ReactNode;
}) {
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  return (
    <div className={`${s.field} ${error ? s.fieldError : ""}`}>
      <div className={s.fieldRow}>
        <label htmlFor={id}>{label}</label>
        {rowExtra}
      </div>
      {children}
      {hint ? (
        <p className={s.hint} id={hintId}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className={s.fieldErr} id={errId}>
          {error}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A password input with a show/hide toggle. Controlled by the parent via
 * `value` / `onChange`. Reveal state is local (presentation only).
 */
export function PasswordInput({
  id,
  value,
  onChange,
  autoComplete,
  placeholder,
  invalid,
  describedBy,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete: string;
  placeholder?: string;
  invalid?: boolean;
  describedBy?: string;
  disabled?: boolean;
}) {
  const tf = useTranslations("auth.fields");
  const [reveal, setReveal] = useState(false);
  return (
    <div className={`${s.control} ${s.controlPw}`}>
      <input
        className={s.input}
        id={id}
        name="password"
        type={reveal ? "text" : "password"}
        autoComplete={autoComplete}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        disabled={disabled}
      />
      <button
        type="button"
        className={s.pwToggle}
        aria-label={reveal ? tf("hidePassword") : tf("showPassword")}
        onClick={() => setReveal((r) => !r)}
        disabled={disabled}
      >
        {reveal ? <EyeOffIcon /> : <EyeIcon />}
      </button>
    </div>
  );
}

const OTP_LENGTH = 6;

/**
 * A 6-digit one-time-code input: six per-digit boxes that behave as one field.
 *
 * - Typing a digit advances focus; Backspace on an empty box steps back.
 * - Pasting a 6-digit code fills all boxes and triggers `onComplete`.
 * - When the full code is present, `onComplete(code)` fires (the parent uses it
 *   to auto-submit). Individually labelled for screen readers; the wrapping
 *   group carries the field-level error styling.
 */
export function OtpInput({
  value,
  onChange,
  onComplete,
  invalid,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  onComplete?: (code: string) => void;
  invalid?: boolean;
  disabled?: boolean;
}) {
  const t = useTranslations("auth.fields");
  const groupId = useId();
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  const digits = Array.from({ length: OTP_LENGTH }, (_, i) => value[i] ?? "");

  const commit = (next: string) => {
    const cleaned = next.replace(/\D/g, "").slice(0, OTP_LENGTH);
    onChange(cleaned);
    if (cleaned.length === OTP_LENGTH) onComplete?.(cleaned);
  };

  const handleChange = (index: number, raw: string) => {
    const char = raw.replace(/\D/g, "").slice(-1);
    if (!char) return;
    const arr = digits.slice();
    arr[index] = char;
    commit(arr.join(""));
    if (index < OTP_LENGTH - 1) refs.current[index + 1]?.focus();
  };

  const handleKeyDown = (
    index: number,
    e: React.KeyboardEvent<HTMLInputElement>,
  ) => {
    if (e.key === "Backspace") {
      if (digits[index]) {
        const arr = digits.slice();
        arr[index] = "";
        commit(arr.join(""));
      } else if (index > 0) {
        refs.current[index - 1]?.focus();
      }
    } else if (e.key === "ArrowLeft" && index > 0) {
      refs.current[index - 1]?.focus();
    } else if (e.key === "ArrowRight" && index < OTP_LENGTH - 1) {
      refs.current[index + 1]?.focus();
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLInputElement>) => {
    const pasted = e.clipboardData
      .getData("text")
      .replace(/\D/g, "")
      .slice(0, OTP_LENGTH);
    if (!pasted) return;
    e.preventDefault();
    commit(pasted);
    const target = Math.min(pasted.length, OTP_LENGTH - 1);
    refs.current[target]?.focus();
  };

  return (
    <fieldset
      className={`${s.field} ${invalid ? s.fieldError : ""}`}
      aria-label={t("verificationCode")}
    >
      <div className={s.otp}>
        {digits.map((digit, i) => (
          <input
            // The boxes are positional and fixed-length; index keys are stable here.
            // biome-ignore lint/suspicious/noArrayIndexKey: fixed-length positional OTP boxes
            key={`${groupId}-${i}`}
            ref={(el) => {
              refs.current[i] = el;
            }}
            inputMode="numeric"
            autoComplete={i === 0 ? "one-time-code" : "off"}
            maxLength={1}
            aria-label={`Digit ${i + 1}`}
            aria-invalid={invalid || undefined}
            value={digit}
            onChange={(e) => handleChange(i, e.target.value)}
            onKeyDown={(e) => handleKeyDown(i, e)}
            onPaste={handlePaste}
            disabled={disabled}
          />
        ))}
      </div>
    </fieldset>
  );
}
