"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { useApi } from "@/lib/api/use-api";

type BillingConfig = components["schemas"]["BillingConfigResponse"];

/**
 * Spec M5 (T3) — plan picker, credit packs, and manage-subscription (D-M5-4/5/6).
 *
 * **Everything renders from the catalog** (`GET /v1/billing/config`, B3), never from
 * hardcoded prices: the owner changes a number in `persona.billing.plans` and this
 * follows. §1c.7 is the failure this avoids — a UI that restates economics drifts from
 * the source of truth the moment the owner edits it.
 *
 * The client posts only a plan CODE or pack CODE. Prices, credit amounts and Stripe
 * Price ids are resolved server-side (`stripe_price_id` / `stripe_price_for_pack` +
 * the pack registry). Nothing money-shaped travels up from the browser, so a tampered
 * request can at worst name a different published plan, never invent a price.
 *
 * Free is rendered as a TIER, never a button (D-M5-4): the backend 400s a `free`
 * checkout, so a button would be a dead end by construction. Same discipline for an
 * unconfigured Price — the backend 400s, and the honest error is surfaced rather than
 * left as a dead spinner (D-M5-3's no-dead-affordance rule applied to failure states).
 */

/** Credits are cents (1 credit = 1¢), so `$` is a pure presentation concern. */
function dollars(credits: number): string {
  return (credits / 100).toFixed(credits % 100 === 0 ? 0 : 2);
}

type Busy = {
  readonly kind: "plan" | "pack" | "portal";
  readonly code: string;
};

export function BillingPlans() {
  const t = useTranslations("billing");
  const api = useApi();
  const [config, setConfig] = useState<BillingConfig | null>(null);
  const [planCode, setPlanCode] = useState<string>("free");
  const [busy, setBusy] = useState<Busy | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [cfg, wallet] = await Promise.all([
          unwrap(await api.GET("/v1/billing/config")),
          unwrap(await api.GET("/v1/me/wallet")),
        ]);
        if (cancelled) return;
        setConfig(cfg);
        setPlanCode(wallet.plan_code);
      } catch {
        // Billing disabled (404) or unreachable: render nothing at all rather than a
        // broken purchase surface. The page's own unmetered copy covers the community
        // case, so a silent absence here is the correct degradation (D-M5-3).
        if (!cancelled) setConfig(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api]);

  /** Redirect to a Stripe-hosted page, or surface the backend's honest refusal. */
  const go = useCallback(
    async (next: Busy, request: () => Promise<{ url: string }>) => {
      setBusy(next);
      setError(null);
      try {
        const { url } = await request();
        window.location.assign(url);
      } catch {
        // The backend 400s an unconfigured Price. Say so plainly and clear the spinner:
        // a stuck spinner is the dead end this rule exists to prevent.
        setError(t("checkoutUnavailable"));
        setBusy(null);
      }
    },
    [t],
  );

  const subscribe = useCallback(
    (code: string) =>
      go({ kind: "plan", code }, async () =>
        unwrap(
          await api.POST("/v1/billing/checkout", {
            body: { plan_code: code as "plus" | "pro" },
          }),
        ),
      ),
    [api, go],
  );

  const buyPack = useCallback(
    (code: string) =>
      go({ kind: "pack", code }, async () =>
        unwrap(
          await api.POST("/v1/billing/checkout/pack", {
            body: { pack: code as "5" | "10" | "25" | "50" },
          }),
        ),
      ),
    [api, go],
  );

  const manage = useCallback(
    () =>
      go({ kind: "portal", code: "portal" }, async () =>
        unwrap(await api.POST("/v1/billing/portal", {})),
      ),
    [api, go],
  );

  // No catalog ⇒ no purchase affordances at all. Never a dead button.
  if (config === null) return null;

  const packExpiry = config.packs[0]?.expiry_months ?? 0;

  return (
    <>
      <Card className="p-6" data-slot="billing-plans">
        <h2 className="type-caption font-mono uppercase text-muted-foreground">
          {t("plansLabel")}
        </h2>
        <div className="mt-4 flex flex-col gap-3">
          {config.plans.map((plan) => {
            const current = plan.code === planCode;
            return (
              <div
                key={plan.code}
                className="flex items-center justify-between gap-4 border-b pb-3 last:border-b-0"
                data-slot="plan-row"
                data-plan={plan.code}
                data-current={String(current)}
              >
                <div>
                  <p className="type-body font-medium">
                    {t("planName", { plan: plan.code })}
                    {current ? ` ${t("currentPlanTag")}` : ""}
                  </p>
                  <p className="type-caption text-muted-foreground">
                    {t("planTerms", {
                      price: dollars(plan.monthly_price_credits),
                      credits: plan.included_allowance_credits,
                    })}
                  </p>
                </div>
                {/* Free is a tier, not a purchase: the backend 400s it, so it never
                    gets a button (D-M5-4). The current plan is managed via the portal. */}
                {plan.monthly_price_credits > 0 && !current ? (
                  <Button
                    disabled={busy !== null}
                    onClick={() => subscribe(plan.code)}
                    data-slot="plan-cta"
                    data-plan={plan.code}
                  >
                    {busy?.kind === "plan" && busy.code === plan.code
                      ? t("opening")
                      : t("choosePlan")}
                  </Button>
                ) : null}
              </div>
            );
          })}
        </div>

        {planCode !== "free" ? (
          <Button
            variant="outline"
            className="mt-4 self-start"
            disabled={busy !== null}
            onClick={manage}
            data-slot="manage-subscription"
          >
            {busy?.kind === "portal" ? t("opening") : t("manageSubscription")}
          </Button>
        ) : null}
      </Card>

      <Card className="p-6" data-slot="billing-packs">
        <h2 className="type-caption font-mono uppercase text-muted-foreground">
          {t("packsLabel")}
        </h2>
        <p className="type-caption mt-2 text-muted-foreground">
          {t("packsHint")}
        </p>
        <div className="mt-4 flex flex-wrap gap-2">
          {config.packs.map((pack) => (
            <Button
              key={pack.code}
              variant="outline"
              disabled={busy !== null}
              onClick={() => buyPack(pack.code)}
              data-slot="pack-cta"
              data-pack={pack.code}
            >
              {busy?.kind === "pack" && busy.code === pack.code
                ? t("opening")
                : t("packLabel", {
                    price: dollars(pack.price_credits),
                    credits: pack.granted_credits,
                  })}
            </Button>
          ))}
        </div>
        {/* D-M5-13: the expiry term is stated AT PURCHASE, not discovered afterwards. */}
        {packExpiry > 0 ? (
          <p
            className="type-caption mt-3 text-muted-foreground"
            data-slot="pack-expiry-note"
          >
            {t("packExpiryNote", { months: packExpiry })}
          </p>
        ) : null}
      </Card>

      {error !== null ? (
        <Card
          className="p-4 ring-1 ring-destructive/30"
          data-slot="billing-error"
        >
          <p className="type-body">{error}</p>
        </Card>
      ) : null}
    </>
  );
}
