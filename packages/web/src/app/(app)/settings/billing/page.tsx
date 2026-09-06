import Link from "next/link";
import { getTranslations } from "next-intl/server";
import { PageBody, PageHeader, Stack } from "@/components/layout";
import { AutoTopupCard } from "@/components/settings/auto-topup-card";
import { BillingPlans } from "@/components/settings/billing-plans";
import { BillingWallet } from "@/components/settings/billing-wallet";
import { PaygLotsCard } from "@/components/settings/payg-lots-card";
import { SubscriptionStatusCard } from "@/components/settings/subscription-status-card";
import { Card } from "@/components/ui/card";
import { serverApi } from "@/lib/api/server";

/**
 * Spec M5 (T1 + T2c) — the billing surface, and the page Stripe sends people back to.
 *
 * This route is deliberately three things at once (D-M5-1): the billing home,
 * the Checkout `success`/`cancel` landing, and the portal return target. Prod
 * already points its Stripe redirects at `/settings/billing?checkout=…`, so this
 * file existing is what turns a completed real payment from a 404 into a page.
 * That is why T1 shipped alone, ahead of the rest of M5.
 *
 * Capability-gated (D-M5-3): the whole `/v1/billing` surface 404s outside
 * cloud + flag + key, so billing is RUNTIME-DISCOVERED, never assumed. When the
 * config read fails we render the unmetered state and show no purchase
 * affordances at all — no dead buttons, no "upgrade" that 404s.
 *
 * Post-checkout honesty (D-M5-2): on `?checkout=success` this page does NOT
 * claim credits were added. Stripe redirects before the webhook necessarily
 * lands, and the grant happens in the webhook. We confirm the payment (a true
 * statement) and hand the balance to `<BillingWallet>`, which re-polls the
 * wallet until it OBSERVES the balance move (the R9-050 honest-loading rule).
 *
 * T2c composes the two caller-state surfaces over `GET /v1/me/wallet` (D-M5-12,
 * the endpoint M4 built for this page): `<SubscriptionStatusCard>` for renewal +
 * the past-due banner (B4/B6), and `<BillingWallet>` for the live balance. The
 * plan picker, packs and portal follow in T3.
 */

interface BillingPageProps {
  searchParams: Promise<{ checkout?: string }>;
}

export default async function BillingPage({ searchParams }: BillingPageProps) {
  const t = await getTranslations("billing");
  const { checkout } = await searchParams;

  // Runtime capability probe. A 404 here is the expected community / flag-off
  // answer, not an error worth surfacing as one — hence the soft fallback.
  // ONE config read, used for both the capability gate and the catalog: it answers
  // "is billing on?" and "what does this plan offer?" in the same payload (D-M5-25),
  // so reading it twice would be two round trips for one fact.
  const api = await serverApi();
  const catalog = await api
    .GET("/v1/billing/config")
    .then((r) => r.data ?? null)
    .catch(() => null);
  const enabled = catalog?.enabled === true;

  // The caller's wallet (D-M5-12) — the source for renewal + past_due. Fail-soft: a
  // wallet error degrades the status card, never the whole page. Only read when billing
  // is on; community has no subscription to describe.
  const wallet = enabled
    ? await api
        .GET("/v1/me/wallet")
        .then((r) => r.data ?? null)
        .catch(() => null)
    : null;

  // The catalog answers whether the caller's plan OFFERS auto-top-up + the owner-locked
  // constants; the wallet answers whether it is armed. Neither is inferred locally
  // (D-M5-11 / D-M5-28). `plans` is optional in the generated type, so this must not
  // assume the array exists — a missing catalog degrades to "no toggle", never a crash.
  const plan = catalog?.plans?.find(
    (p) => p.code === (wallet?.plan_code ?? "free"),
  );

  return (
    <PageBody>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />

      <Stack gap={5}>
        {checkout === "success" ? (
          <Card
            className="p-6 ring-1 ring-primary/40"
            data-slot="checkout-success"
          >
            <h2 className="type-heading">{t("successTitle")}</h2>
            <p className="type-body mt-2 text-muted-foreground">
              {t("successBody")}
            </p>
          </Card>
        ) : null}

        {checkout === "cancel" ? (
          <Card className="p-6" data-slot="checkout-cancel">
            <h2 className="type-heading">{t("cancelTitle")}</h2>
            <p className="type-body mt-2 text-muted-foreground">
              {t("cancelBody")}
            </p>
          </Card>
        ) : null}

        {wallet ? <SubscriptionStatusCard wallet={wallet} /> : null}

        {enabled ? (
          <>
            <BillingWallet awaitingGrant={checkout === "success"} />
            {wallet && catalog ? (
              <AutoTopupCard
                planCode={wallet.plan_code}
                eligible={plan?.auto_topup_eligible ?? false}
                initialEnabled={wallet.auto_topup_enabled}
                thresholdCredits={catalog.auto_topup_threshold_credits}
                amountCredits={catalog.auto_topup_amount_credits}
              />
            ) : null}
            <BillingPlans />
            {wallet ? <PaygLotsCard lots={wallet.payg_lots} /> : null}
          </>
        ) : (
          <Card className="p-6" data-slot="billing-overview">
            <h2 className="type-caption font-mono uppercase text-muted-foreground">
              {t("planLabel")}
            </h2>
            <p className="type-body mt-3 max-w-prose text-muted-foreground">
              {t("unmeteredBody")}
            </p>
          </Card>
        )}

        <p className="type-caption text-muted-foreground">
          <Link className="underline underline-offset-4" href="/settings">
            {t("backToSettings")}
          </Link>
        </p>
      </Stack>
    </PageBody>
  );
}
