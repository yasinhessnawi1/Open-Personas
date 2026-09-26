/**
 * Which sentence a plan row and a pack button say (R9-177 B6, owner ruling 2026-09-26).
 *
 * The extra a plan gives is COMPUTED from the catalog (allowance minus price) and only
 * claimed when it is positive. These pin the selection and the exact values; the
 * rendered sentences are pinned in `billing-plans.test.tsx`.
 */

import { describe, expect, it } from "vitest";
import { packTerms, planTerms } from "./billing-terms";

describe("planTerms", () => {
  it("uses the free line for a plan that costs nothing", () => {
    expect(
      planTerms({ monthly_price_credits: 0, included_allowance_credits: 300 }),
    ).toEqual({ key: "planTermsFree", values: { credit: "$3" } });
  });

  it.each([
    [1500, 2000, { credit: "$20", price: "$15", extra: "$5" }],
    [5000, 6000, { credit: "$60", price: "$50", extra: "$10" }],
    [1500, 1550, { credit: "$15.50", price: "$15", extra: "$0.50" }],
  ])(
    "claims the extra when the allowance beats the price (%i for %i)",
    (price, allowance, values) => {
      expect(
        planTerms({
          monthly_price_credits: price,
          included_allowance_credits: allowance,
        }),
      ).toEqual({ key: "planTermsExtra", values });
    },
  );

  it.each([
    [1500, 1500],
    [1500, 1200],
  ])(
    "makes no extra claim when the allowance does not beat the price (%i for %i)",
    (price, allowance) => {
      const terms = planTerms({
        monthly_price_credits: price,
        included_allowance_credits: allowance,
      });
      expect(terms.key).toBe("planTermsPlain");
      expect(terms.values).not.toHaveProperty("extra");
    },
  );
});

describe("packTerms", () => {
  it("names one amount when the pack grants exactly its price", () => {
    expect(packTerms({ price_credits: 1000, granted_credits: 1000 })).toEqual({
      key: "packLabel",
      values: { amount: "$10" },
    });
  });

  it("names both amounts when the pack grants something else", () => {
    expect(packTerms({ price_credits: 1000, granted_credits: 1250 })).toEqual({
      key: "packLabelPriced",
      values: { price: "$10", credit: "$12.50" },
    });
  });
});
