/**
 * The billing formatter attaches the unit to the number (R9-177 B6).
 *
 * One credit is one US cent, so the formatter is the whole of the credit-to-dollar
 * presentation on the billing surfaces. Whole dollars print bare, anything else with
 * two decimals, and zero is "$0", never "$0.00" or an empty string.
 */

import { describe, expect, it } from "vitest";
import { usdFromCredits } from "./money";

describe("usdFromCredits", () => {
  it.each([
    [1500, "$15"],
    [6000, "$60"],
    [100, "$1"],
  ])("prints whole dollars without cents (%i credits)", (credits, expected) => {
    expect(usdFromCredits(credits)).toBe(expected);
  });

  it.each([
    [1550, "$15.50"],
    [2350, "$23.50"],
    [5, "$0.05"],
    [199, "$1.99"],
  ])("prints cents with two decimals (%i credits)", (credits, expected) => {
    expect(usdFromCredits(credits)).toBe(expected);
  });

  it("prints zero as $0", () => {
    expect(usdFromCredits(0)).toBe("$0");
  });

  it.each([
    [123_450, "$1,234.50"],
    [1_240_000, "$12,400"],
    [100_000_000, "$1,000,000"],
    [123_456_789, "$1,234,567.89"],
  ])("groups thousands (%i credits)", (credits, expected) => {
    expect(usdFromCredits(credits)).toBe(expected);
  });
});
