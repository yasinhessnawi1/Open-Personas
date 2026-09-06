"use client";

import { useFormatter, useTranslations } from "next-intl";
import { Card } from "@/components/ui/card";
import type { components } from "@/lib/api/schema";

type Wallet = components["schemas"]["WalletResponse"];
type PaygLot = components["schemas"]["PaygLotOut"];

/**
 * Spec M5 (T5) — the PAYG lots a user bought, and when each one lapses (D-M5-13).
 *
 * Credit packs expire 12 months after purchase (D-M4-5). M4 built the lots, the expiry
 * dates and the FIFO spend order, and `GET /v1/me/wallet` has served them since T2a, but
 * nothing rendered them (§1c.9). So a user could buy $50 of credits and watch them
 * disappear a year later with no warning and no way to have seen it coming.
 *
 * Bounding liability with an expiry is legitimate. Doing it silently is not. This shows:
 *
 *   - what remains in each lot, and what it started as, so partial spend is visible;
 *   - when each lot expires, as a real date;
 *   - the lots in the order they will actually be spent (FIFO, oldest-expiring first,
 *     exactly as the server returns them — never re-sorted here, or the page would claim
 *     a spend order the ledger does not follow);
 *   - a warning on any lot close enough to lapse that the user can still act on it.
 *
 * The wallet is fetched once by the page and passed down, so this never re-reads the same
 * fact.
 */

/** Days before expiry at which a lot is worth warning about. */
const EXPIRY_WARNING_DAYS = 30;

const MS_PER_DAY = 86_400_000;

export function daysUntil(expiresAt: string, now: number): number {
  return Math.ceil((Date.parse(expiresAt) - now) / MS_PER_DAY);
}

export interface PaygLotsCardProps {
  readonly lots: Wallet["payg_lots"];
  /** Injected in tests so "expiring soon" is deterministic rather than clock-dependent. */
  readonly now?: number;
}

export function PaygLotsCard({ lots, now = Date.now() }: PaygLotsCardProps) {
  const t = useTranslations("billing");
  const format = useFormatter();

  // No packs bought: say nothing rather than render an empty box. The packs card above
  // already invites the first purchase.
  if (lots.length === 0) return null;

  return (
    <Card className="p-6" data-slot="payg-lots">
      <h2 className="type-caption font-mono uppercase text-muted-foreground">
        {t("lotsLabel")}
      </h2>
      <p className="type-caption mt-2 text-muted-foreground">{t("lotsHint")}</p>

      <ul className="mt-4 flex flex-col gap-3">
        {lots.map((lot: PaygLot) => {
          const days = daysUntil(lot.expires_at, now);
          const expiringSoon = days <= EXPIRY_WARNING_DAYS;
          return (
            <li
              key={`${lot.expires_at}-${lot.credits_total}`}
              className="flex items-center justify-between gap-4 border-b pb-3 last:border-b-0"
              data-slot="payg-lot"
              data-expiring-soon={String(expiringSoon)}
            >
              <div>
                <p className="type-body font-medium">
                  {t("lotRemaining", {
                    remaining: lot.credits_remaining,
                    total: lot.credits_total,
                  })}
                </p>
                <p className="type-caption text-muted-foreground">
                  {t("lotExpires", {
                    date: format.dateTime(new Date(lot.expires_at), {
                      year: "numeric",
                      month: "long",
                      day: "numeric",
                    }),
                  })}
                </p>
              </div>
              {expiringSoon ? (
                <p className="type-caption" data-slot="payg-lot-warning">
                  {t("lotExpiringSoon", { days: Math.max(days, 0) })}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
