"use client";

import { useFormatter, useTranslations } from "next-intl";
import { Card } from "@/components/ui/card";
import type { components } from "@/lib/api/schema";

type Wallet = components["schemas"]["WalletResponse"];

/**
 * Spec M5 (T2c) — renewal truth + the past-due banner (D-M5-10 / D-M5-14, over B4 + B6).
 *
 * Two questions a paying customer asks that the app previously could not answer, because
 * the data was persisted but exposed in no response schema:
 *
 * 1. **When does this renew?** `current_period_end`, plus whether the plan is set to stop
 *    at that date. Both facts are stated together — "cancelling" alone reads as if access
 *    ends now, and a renewal date alone hides that it is the last one.
 * 2. **Why am I running dry?** A failed subscription payment flips the subscription to
 *    `past_due` and the allowance quietly stops renewing (no `invoice.paid` ⇒ no reset).
 *    Before this, nothing told the user. The banner says plainly that the payment failed
 *    and what it means, and sends them to the Stripe portal (D-M5-6) to fix the card.
 *    §5 rules out dunning EMAIL; this is the in-app channel, which is not the same thing.
 *
 * Presentational: the wallet is fetched once by the page and passed down, so this never
 * triggers a second read of the same fact.
 */
export interface SubscriptionStatusCardProps {
  readonly wallet: Pick<
    Wallet,
    | "plan_code"
    | "current_period_end"
    | "cancel_at_period_end"
    | "subscription_status"
  >;
  /** Opens the Stripe billing portal (D-M5-6); absent while the portal action is not wired. */
  readonly onManage?: () => void;
}

export function SubscriptionStatusCard({
  wallet,
  onManage,
}: SubscriptionStatusCardProps) {
  const t = useTranslations("billing");
  const format = useFormatter();
  const pastDue = wallet.subscription_status === "past_due";
  // The generated field is optional AND nullable (`string | null | undefined`), so
  // normalise once here rather than re-narrowing at each use.
  const renewsAt = wallet.current_period_end ?? null;

  // A free caller has no subscription: nothing renews and nothing can be past due, so the
  // card would be an empty box making a claim about a plan they do not have.
  if (!pastDue && renewsAt === null) return null;

  return (
    <Card
      className={pastDue ? "p-6 ring-1 ring-destructive/30" : "p-6"}
      data-slot="subscription-status"
      data-status={wallet.subscription_status}
    >
      {pastDue ? (
        <>
          <h2 className="type-heading" data-slot="past-due-title">
            {t("pastDueTitle")}
          </h2>
          <p className="type-body mt-2 text-muted-foreground">
            {t("pastDueBody")}
          </p>
          {onManage ? (
            <button
              type="button"
              onClick={onManage}
              className="type-ui mt-3 self-start underline underline-offset-4"
              data-slot="past-due-cta"
            >
              {t("managePayment")}
            </button>
          ) : null}
        </>
      ) : (
        <>
          <h2 className="type-caption font-mono uppercase text-muted-foreground">
            {wallet.cancel_at_period_end ? t("endsLabel") : t("renewsLabel")}
          </h2>
          <p className="type-body mt-2" data-slot="renewal-date">
            {renewsAt === null
              ? t("renewalUnknown")
              : format.dateTime(new Date(renewsAt), {
                  year: "numeric",
                  month: "long",
                  day: "numeric",
                })}
          </p>
          {wallet.cancel_at_period_end ? (
            <p
              className="type-caption mt-1 text-muted-foreground"
              data-slot="cancelling-note"
            >
              {t("cancellingNote")}
            </p>
          ) : null}
        </>
      )}
    </Card>
  );
}
