/**
 * No money string writes its own currency sign (R9-177 B6, the R9-172 rule).
 *
 * Every wallet figure the app shows, on the billing page, the settings balance, the
 * home balance tile and the low-balance notification, arrives already formatted by
 * `usdFromCredits`, which attaches the `$`. A sign written in the copy beside a
 * placeholder is a place where the unit and the number can disagree, which is exactly
 * how the task surface rendered dollars with a kroner label for months.
 */

import { describe, expect, it } from "vitest";
import messages from "@/i18n/messages/en.json";

/** The namespaces whose strings carry wallet figures. */
const MONEY_NAMESPACES = [
  "billing",
  "settings",
  "home",
  "notifications",
] as const;

/** Every string in a namespace, recursively, as `[dotted path, text]`. */
function strings(node: unknown, path: string): [string, string][] {
  if (typeof node === "string") return [[path, node]];
  if (node && typeof node === "object") {
    return Object.entries(node).flatMap(([key, value]) =>
      strings(value, `${path}.${key}`),
    );
  }
  return [];
}

const entries = MONEY_NAMESPACES.flatMap((ns) => strings(messages[ns], ns));

describe("money copy", () => {
  it.each(MONEY_NAMESPACES)("has strings to check in %s", (ns) => {
    // A check over an empty collection passes while seeing nothing.
    expect(strings(messages[ns], ns).length).toBeGreaterThan(3);
  });

  it.each(entries)("%s carries no currency sign of its own", (_key, text) => {
    expect(text).not.toMatch(/[$€£¥]/);
  });
});
