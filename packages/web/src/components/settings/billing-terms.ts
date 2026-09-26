import { usdFromCredits } from "@/lib/money";

/**
 * Which billing sentence a plan row and a pack button say, and the values it takes
 * (R9-177 B6, owner ruling 2026-09-26).
 *
 * One unit, dollars, and every number from the catalog through {@link usdFromCredits},
 * so the unit is attached by the formatter and never written in the copy. The "extra"
 * a plan gives over its price is COMPUTED here from the catalog, never written as a
 * number anywhere: if the owner reprices a plan, the claim follows the catalog, and if
 * the extra ever reaches zero the claim disappears instead of going stale.
 */

type PlanEconomics = {
  readonly monthly_price_credits: number;
  readonly included_allowance_credits: number;
};

type PackEconomics = {
  readonly price_credits: number;
  readonly granted_credits: number;
};

export type PlanTerms =
  | { readonly key: "planTermsFree"; readonly values: { credit: string } }
  | {
      readonly key: "planTermsExtra";
      readonly values: { credit: string; price: string; extra: string };
    }
  | {
      readonly key: "planTermsPlain";
      readonly values: { credit: string; price: string };
    };

export type PackTerms =
  | { readonly key: "packLabel"; readonly values: { amount: string } }
  | {
      readonly key: "packLabelPriced";
      readonly values: { price: string; credit: string };
    };

/**
 * The plan line: free, a paid plan that gives more than it costs, or a plain paid plan.
 */
export function planTerms(plan: PlanEconomics): PlanTerms {
  const credit = usdFromCredits(plan.included_allowance_credits);
  if (plan.monthly_price_credits === 0) {
    return { key: "planTermsFree", values: { credit } };
  }
  const price = usdFromCredits(plan.monthly_price_credits);
  const extraCredits =
    plan.included_allowance_credits - plan.monthly_price_credits;
  if (extraCredits > 0) {
    return {
      key: "planTermsExtra",
      values: { credit, price, extra: usdFromCredits(extraCredits) },
    };
  }
  return { key: "planTermsPlain", values: { credit, price } };
}

/**
 * The pack button. A pack grants exactly its price today, so one figure is the whole
 * truth ("Add $10"). If the catalog ever grants a different amount, both figures show.
 */
export function packTerms(pack: PackEconomics): PackTerms {
  if (pack.granted_credits === pack.price_credits) {
    return {
      key: "packLabel",
      values: { amount: usdFromCredits(pack.price_credits) },
    };
  }
  return {
    key: "packLabelPriced",
    values: {
      price: usdFromCredits(pack.price_credits),
      credit: usdFromCredits(pack.granted_credits),
    },
  };
}
