import Link from "next/link";
import { getTranslations } from "next-intl/server";
import { PageBody, PageHeader, Stack } from "@/components/layout";
import { Card } from "@/components/ui/card";
import { serverApi } from "@/lib/api/server";

/**
 * Spec M5 (T1) — the billing surface, and the page Stripe sends people back to.
 *
 * This route is deliberately three things at once (D-M5-1): the billing home,
 * the Checkout `success`/`cancel` landing, and the portal return target. Prod
 * already points its Stripe redirects at `/settings/billing?checkout=…`, so this
 * file existing is what turns a completed real payment from a 404 into a page.
 * That is why T1 ships alone, ahead of the rest of M5.
 *
 * Capability-gated (D-M5-3): the whole `/v1/billing` surface 404s outside
 * cloud + flag + key, so billing is RUNTIME-DISCOVERED, never assumed. When the
 * config read fails we render the unmetered state and show no purchase
 * affordances at all — no dead buttons, no "upgrade" that 404s.
 *
 * Post-checkout honesty (D-M5-2): on `?checkout=success` this page does NOT
 * claim credits were added. Stripe redirects before the webhook necessarily
 * lands, and the grant happens in the webhook. We confirm the payment (a true
 * statement) and say the balance follows shortly. The wallet-backed live
 * balance + bounded re-poll arrive in T2c; until then this page states only
 * what it actually knows, which is the R9-050 honest-loading rule.
 */

interface BillingPageProps {
  searchParams: Promise<{ checkout?: string }>;
}

export default async function BillingPage({ searchParams }: BillingPageProps) {
  const t = await getTranslations("billing");
  const { checkout } = await searchParams;

  // Runtime capability probe. A 404 here is the expected community / flag-off
  // answer, not an error worth surfacing as one — hence the soft fallback.
  const api = await serverApi();
  const enabled = await api
    .GET("/v1/billing/config")
    .then((r) => r.data?.enabled === true)
    .catch(() => false);

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

        <Card className="p-6" data-slot="billing-overview">
          <h2 className="type-caption font-mono uppercase text-muted-foreground">
            {t("planLabel")}
          </h2>
          <p className="type-body mt-3 max-w-prose text-muted-foreground">
            {enabled ? t("enabledIntro") : t("unmeteredBody")}
          </p>
        </Card>

        <p className="type-caption text-muted-foreground">
          <Link className="underline underline-offset-4" href="/settings">
            {t("backToSettings")}
          </Link>
        </p>
      </Stack>
    </PageBody>
  );
}
