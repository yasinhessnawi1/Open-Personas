import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";

/**
 * Spec F? L6a — Low-balance inline warning.
 *
 * Renders an inline warning Card when the backend `CreditsResponse.low_balance`
 * flag is true AND `balance > 0`. The credits-exhausted cliff (balance === 0)
 * is a separate surface, handled by the page's existing
 * `<ErrorState status={402}>` branch.
 *
 * Pure presentational — strings are pre-resolved by the (server) page caller
 * via `getTranslations("settings")`, keeping this component client-safe and
 * trivially testable.
 *
 * Spec M5 (T6): the optional `action` slot. This card used to state the problem
 * and stop there (§1c.2), which is a dead end: it told a user their balance was
 * running out on a page with nothing to do about it. The caller now passes a
 * link to `/settings/billing`, where credits can actually be bought. Optional so
 * the card stays usable anywhere a CTA would be wrong, and so existing callers
 * keep working unchanged.
 */
export interface LowBalanceWarningCardProps {
  readonly credits: { readonly balance: number; readonly low_balance: boolean };
  readonly title: string;
  readonly hint: string;
  /** Where to go about it. Omitted renders the card exactly as before. */
  readonly action?: ReactNode;
}

export function LowBalanceWarningCard({
  credits,
  title,
  hint,
  action,
}: LowBalanceWarningCardProps) {
  if (!credits.low_balance || credits.balance <= 0) {
    return null;
  }
  return (
    <Card
      className="gap-1 p-4 ring-tier-mid/40"
      data-slot="settings-low-balance-warning"
    >
      <p className="type-body font-medium">{title}</p>
      <p className="type-caption text-muted-foreground">{hint}</p>
      {action ? (
        <div className="mt-2" data-slot="settings-low-balance-action">
          {action}
        </div>
      ) : null}
    </Card>
  );
}
