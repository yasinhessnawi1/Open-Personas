/**
 * The one place the browser turns a stored money integer into something a person reads.
 *
 * This product has exactly one currency and it is USD. That is not a preference: the
 * currency audit of 2026-09-14 found no currency column and no rate column on any money
 * table in the schema, so a completed charge does not record what the customer was billed
 * or in what currency. Rendering a stored amount in a local currency at today's rate would
 * silently restate the value of every past transaction on every page load, with nothing in
 * the database able to contradict it.
 *
 * Until 2026-09-15 the task surface rendered dollars with a kroner label, because the
 * divisor 10 000 is micros per DOLLAR and the formatter was called `kr` (R9-172). The
 * arithmetic was right, the word was wrong, and no test could catch a word.
 *
 * So the unit is attached to the value here rather than written beside it in a translation
 * string. A template like `"kr {spent} / {cap}"` lets the unit and the number drift apart;
 * a formatter that returns `"$1.50"` does not.
 */

/** Ledger micros per US dollar. Mirrors `persona.tasks.MICROS_PER_DOLLAR` in core. */
export const MICROS_PER_DOLLAR = 10_000;

/** Credits per US dollar. A credit is one US cent (M3), so `$` is pure presentation. */
export const CREDITS_PER_DOLLAR = 100;

/** micros → `"$12"` / `"$1.5"`, unit attached; sub-$10 keeps one decimal. */
export function usd(micros: number): string {
  const v = micros / MICROS_PER_DOLLAR;
  return `$${v < 10 ? v.toFixed(1) : Math.round(v).toString()}`;
}

/**
 * micros → `"$0.64"`, for spends that are routinely sub-dollar.
 *
 * An overnight run costs a few cents, and {@link usd} would flatten that to `"$0.1"`.
 */
export function usdFine(micros: number): string {
  const v = micros / MICROS_PER_DOLLAR;
  return v < 1 ? `$${v.toFixed(2)}` : usd(micros);
}

/** A typed dollar amount → micros, for the one money input field in the app. */
export function microsFromDollars(input: string | number): number {
  return Math.round(Number(input) * MICROS_PER_DOLLAR);
}

/**
 * The locale wallet figures are formatted in. The app's catalogue is English and USD is
 * its only currency, so this is fixed until currency exchange is done properly (phase 4).
 */
const MONEY_LOCALE = "en-US";

/** Built once each: an `Intl.NumberFormat` is immutable, and constructing one is not free. */
const WHOLE_DOLLARS = new Intl.NumberFormat(MONEY_LOCALE, {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});
const DOLLARS_AND_CENTS = new Intl.NumberFormat(MONEY_LOCALE, {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/**
 * credits → `"$15"` / `"$15.50"` / `"$0"` / `"$1,234.50"`, unit attached. The one
 * formatter every wallet figure in the app goes through.
 *
 * Whole dollars print without cents, anything else with two decimals, thousands grouped.
 * Every money string takes the formatted value and carries no `$` of its own (R9-177
 * B6): until 2026-09-26 the copy wrote the sign beside a bare number
 * (`"${price} a month"`), which is the same drift R9-172 was, one layer up.
 */
export function usdFromCredits(credits: number): string {
  const formatter =
    credits % CREDITS_PER_DOLLAR === 0 ? WHOLE_DOLLARS : DOLLARS_AND_CENTS;
  return formatter.format(credits / CREDITS_PER_DOLLAR);
}
