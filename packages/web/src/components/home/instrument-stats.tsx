import { getFormatter, getTranslations } from "next-intl/server";
import { usdFromCredits } from "@/lib/money";

/**
 * Dashboard instrument stats (Spec 35 D-35-1) — the `.v-stat` tile row.
 *
 * Wired to REAL data only (the spec forbids faking; the mockup's literal
 * "1,204 / 6 / 2" were hardcoded):
 *   - Balance: the caller's balance (`/v1/me/credits`), in dollars through
 *     `usdFromCredits` (R9-177 B6, owner ruling 2026-09-26). Community is
 *     unmetered → the balance is a sentinel, so it degrades to "Unlimited". A
 *     failed/absent fetch hides the tile rather than guessing.
 *   - Active personas: the already-fetched personas list length (free).
 *   - Conversations: the already-fetched conversation count (free). This stands
 *     in for the mockup's "runs in progress" — there is no list-runs endpoint to
 *     count active runs, and faking it is out (a per-run count would be an N+1).
 *
 * Server component — values are resolved by the page and passed in.
 */
export async function InstrumentStats({
  credits,
  personaCount,
  conversationCount,
  edition,
}: {
  /** The caller's credit balance, or null when unavailable (hide the tile). */
  credits: number | null;
  personaCount: number;
  conversationCount: number;
  edition: "cloud" | "community";
}) {
  const t = await getTranslations("home.stats");
  const format = await getFormatter();

  const creditsValue =
    edition === "community"
      ? t("creditsUnlimited")
      : credits !== null
        ? usdFromCredits(credits)
        : null;

  return (
    <div className="v-grid v-grid--3">
      {creditsValue !== null ? (
        <div className="v-card v-stat">
          <div className="v-stat__label">{t("balance")}</div>
          <div className="v-stat__value">{creditsValue}</div>
        </div>
      ) : null}
      <div className="v-card v-stat">
        <div className="v-stat__label">{t("personas")}</div>
        <div className="v-stat__value">{format.number(personaCount)}</div>
      </div>
      <div className="v-card v-stat">
        <div className="v-stat__label">{t("conversations")}</div>
        <div className="v-stat__value">{format.number(conversationCount)}</div>
      </div>
    </div>
  );
}
