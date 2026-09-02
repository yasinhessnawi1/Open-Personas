/**
 * Shared branded auth shell + brand panel (Spec 34, Cluster A) — cloud-only.
 *
 * The split-layout chrome ported from the design prototype (D-34-2:
 * brand-panel-left / form-right; collapses to a compact brand header above the
 * form below ~720px). The left brand panel is theme-swapped (the stacked logo
 * lockup flips dark/light), shows the four typed-memory store dots, and carries
 * sign-in / sign-up specific copy via props. The right side renders the flow
 * form passed as `children`.
 *
 * This component is presentation only — it holds no Clerk state. It is imported
 * exclusively by the cloud sign-in / sign-up / reset components, so it never
 * enters the community module graph.
 */
import Image from "next/image";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import styles from "./auth-shell.module.css";

/** Copy shown in the left brand panel (differs per flow). */
export interface BrandCopy {
  /** Mono kicker above the tagline, e.g. `"Typed-memory AI"`. */
  readonly kicker: string;
  /** Display-face tagline. */
  readonly tagline: string;
  /** Supporting note under the tagline. */
  readonly note: string;
  /** Compact one-liner shown when the panel collapses on mobile. */
  readonly compact: string;
}

/** The four typed-memory stores rendered as labelled colour dots; the labels
 * come from `auth.stores.<key>` so the panel translates with the locale. */
const STORE_DOTS = [
  { key: "identity", color: "#2bb6aa" },
  { key: "self", color: "#5bb05a" },
  { key: "worldview", color: "#8f8bf2" },
  { key: "episodic", color: "#e873a6" },
] as const;

function BrandPanel({ copy }: { copy: BrandCopy }) {
  const t = useTranslations("auth.stores");
  return (
    <aside className={styles.brand}>
      <div className={styles.brandTop}>
        {/* Theme-swapped stacked lockup: -light wordmark on light surfaces,
         * -dark on dark. next-themes toggles `.dark` on <html>; the global
         * `dark:` styling is mirrored by rendering both and hiding one. */}
        <Image
          src="/brand/logo-lockup-stacked-light.svg"
          alt="Open Persona"
          width={156}
          height={101}
          className={`${styles.brandLockup} dark:hidden`}
          priority
        />
        <Image
          src="/brand/logo-lockup-stacked-dark.svg"
          alt="Open Persona"
          width={156}
          height={101}
          className={`${styles.brandLockup} hidden dark:block`}
          priority
        />
      </div>

      <div className={styles.spacer} />
      <p className={styles.kicker}>{copy.kicker}</p>
      <p className={styles.tagline}>{copy.tagline}</p>
      <p className={styles.note}>{copy.note}</p>
      <div className={styles.spacer} />

      <div className={styles.stores}>
        {STORE_DOTS.map((store) => (
          <span key={store.key}>
            <i className={styles.dot} style={{ background: store.color }} />
            {t(store.key)}
          </span>
        ))}
      </div>

      <p className={styles.brandCompact}>{copy.compact}</p>
    </aside>
  );
}

/** Props for the auth shell: brand copy plus the form rendered on the right. */
export interface AuthShellProps {
  readonly brand: BrandCopy;
  readonly children: ReactNode;
}

/**
 * The branded auth shell: brand panel on the left, the flow form on the right.
 */
export function AuthShell({ brand, children }: AuthShellProps) {
  return (
    <div className={styles.auth}>
      <BrandPanel copy={brand} />
      <div className={styles.form}>{children}</div>
    </div>
  );
}

/** Re-export the styles so the flow components share one CSS module. */
export { styles as authStyles };
