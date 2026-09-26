/**
 * The home balance tile speaks dollars (R9-177 B6, owner ruling 2026-09-26).
 *
 * A server component, rendered through the real catalogue with `next-intl/server`
 * stubbed to read it. The community edition is unmetered, so its sentinel balance must
 * never be shown as money: it reads "Unlimited".
 */

import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { InstrumentStats } from "./instrument-stats";

vi.mock("next-intl/server", () => ({
  getTranslations: (ns: string) => {
    const table = ns
      .split(".")
      .reduce<unknown>(
        (node, key) => (node as Record<string, unknown>)[key],
        messages,
      ) as Record<string, string>;
    return Promise.resolve((key: string) => table[key] ?? `${ns}.${key}`);
  },
  getFormatter: () => Promise.resolve({ number: (n: number) => String(n) }),
}));

async function balanceTile(
  credits: number | null,
  edition: "cloud" | "community",
): Promise<{ label: string; value: string } | null> {
  render(
    await InstrumentStats({
      credits,
      personaCount: 2,
      conversationCount: 3,
      edition,
    }),
  );
  const tile = document.querySelectorAll(".v-stat")[0];
  const label = tile?.querySelector(".v-stat__label")?.textContent ?? "";
  if (label !== "Balance") return null;
  return {
    label,
    value: tile?.querySelector(".v-stat__value")?.textContent ?? "",
  };
}

describe("InstrumentStats balance tile", () => {
  it("shows the cloud balance in dollars", async () => {
    expect(await balanceTile(1850, "cloud")).toEqual({
      label: "Balance",
      value: "$18.50",
    });
  });

  it("shows the community balance as Unlimited, never as money", async () => {
    expect(await balanceTile(1_000_000_000, "community")).toEqual({
      label: "Balance",
      value: "Unlimited",
    });
  });

  it("hides the tile when the balance could not be read", async () => {
    expect(await balanceTile(null, "cloud")).toBeNull();
  });
});
