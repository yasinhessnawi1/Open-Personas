"use client";

import { useLocale, useTranslations } from "next-intl";

import { kr } from "@/components/tasks/task-row";
import type { MorningDigest } from "@/lib/api/review-client";

/**
 * Spec R11 (B2) — the Activity dateline, in the ratified A6-R-1 "Morning
 * Review" register (owner-locked over the kit's "Good morning."):
 *
 *     Thursday morning.
 *     *Three things* need you.
 *     5 Jul · 07:00  ·  3 personas worked overnight  ·  kr 0.64 spent
 *
 * Editorial Fraunces headline; the needs-you count is the ONE accent moment
 * (italic, terracotta) — loud only where it informs. Every number is honest:
 * the count sums waiting + stuck items INCLUDING each section's overflow
 * ("+N more" beyond the one-minute cap still needs you), worked = distinct
 * personas across all sections, and the spend segment renders only when
 * something was actually spent.
 */

/** waiting + stuck items (incl. honest overflow) — the count that "needs you". */
export function needsYouCount(digest: MorningDigest): number {
  return digest.sections
    .filter((s) => s.kind === "waiting" || s.kind === "stuck")
    .reduce((n, s) => n + s.items.length + s.overflow, 0);
}

/** Distinct personas that show up anywhere in the digest. */
export function personasWorkedCount(digest: MorningDigest): number {
  const ids = new Set<string>();
  for (const section of digest.sections)
    for (const item of section.items) ids.add(item.persona_id);
  return ids.size;
}

/** Overnight spends are often sub-1kr; keep two decimals there (the artifact's
 * "kr 0.64 spent") where the shared `kr()` would flatten it to one. */
function krFine(micros: number): string {
  const v = micros / 10_000;
  return v < 1 ? v.toFixed(2) : kr(micros);
}

function daypartKey(
  hour: number,
): "night" | "morning" | "afternoon" | "evening" {
  if (hour < 5) return "night";
  if (hour < 12) return "morning";
  if (hour < 18) return "afternoon";
  return "evening";
}

export function ReviewDateline({ digest }: { digest: MorningDigest }) {
  const t = useTranslations("review.dateline");
  const locale = useLocale();

  const at = new Date(digest.generated_at);
  const weekday = at.toLocaleDateString(locale, { weekday: "long" });
  const date = at.toLocaleDateString(locale, {
    day: "numeric",
    month: "short",
  });
  const time = at.toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  const needs = needsYouCount(digest);
  const worked = personasWorkedCount(digest);

  const meta: string[] = [`${date} · ${time}`];
  if (worked > 0) meta.push(t("worked", { count: worked }));
  if (digest.total_spent_micros > 0)
    meta.push(t("spent", { amount: krFine(digest.total_spent_micros) }));

  return (
    <div className="flex flex-col gap-3" data-slot="review-dateline">
      <h1 className="font-heading text-3xl font-normal leading-[1.15] tracking-tight text-balance">
        {t("moment", {
          weekday,
          daypart: t(`daypart.${daypartKey(at.getHours())}`),
        })}
        <br />
        {needs === 0
          ? t("nothing")
          : t.rich("needs", {
              count: needs,
              em: (chunks) => <em className="italic text-primary">{chunks}</em>,
            })}
      </h1>
      <p className="type-caption flex flex-wrap items-center gap-x-2 gap-y-1 text-muted-foreground">
        {meta.map((segment, i) => (
          <span key={segment} className="flex items-center gap-2">
            {i > 0 ? (
              <span
                aria-hidden="true"
                className="size-[3px] rounded-full bg-muted-foreground/60"
              />
            ) : null}
            {segment}
          </span>
        ))}
      </p>
    </div>
  );
}
