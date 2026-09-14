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
 * credits → `"15"` / `"15.50"`, WITHOUT the `$`.
 *
 * The sign is separate here because the billing strings carry it in the copy
 * (`"${price} a month"`). This was two byte-identical copies in `billing-plans.tsx` and
 * `auto-topup-card.tsx` until 2026-09-15; two copies of a money conversion drift.
 */
export function dollarsFromCredits(credits: number): string {
  return (credits / CREDITS_PER_DOLLAR).toFixed(
    credits % CREDITS_PER_DOLLAR === 0 ? 0 : 2,
  );
}
