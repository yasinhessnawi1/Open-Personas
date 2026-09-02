"use client";

import { useTranslations } from "next-intl";
import { TIER_BADGE_SETTING, useBoolSetting } from "@/lib/hooks/use-setting";
import type { RoutingSummary } from "@/lib/sse-types";
import { cn } from "@/lib/utils";

// Tier tokens escalate cool→hot (small=slate · mid=amber · frontier=vermilion),
// making the routing layer tangible (spec §4.1). F1 T14 confirmed chroma —
// not lightness — carries the firepower signal.
const TIER_CLASS: Record<string, string> = {
  frontier: "border-tier-frontier/40 text-tier-frontier",
  mid: "border-tier-mid/50 text-tier-mid",
  small: "border-tier-small/50 text-tier-small",
};

const CHIP =
  "type-caption inline-flex w-fit items-center rounded border px-1.5 py-0.5";

/** Strip the provider prefix for a compact chip label (anthropic/good → good). */
function shortModel(model: string): string {
  const slash = model.lastIndexOf("/");
  return slash >= 0 ? model.slice(slash + 1) : model;
}

/**
 * TierBadge (Spec 31 expansion, D-31-1). Without `routing` it renders exactly
 * as before — a bare tier chip (back-compat for rule-based turns). With
 * `routing`, it becomes a progressive-disclosure chip: the summary shows the
 * tier + chosen model; expanding reveals *why* (the dominant-factor reason, or
 * an honest "tier default — live data unavailable" when routing fell back). The
 * raw score vector is never shown — it stays in the audit JSONL.
 */
export function TierBadge({
  tier,
  routing,
}: {
  tier: string;
  routing?: RoutingSummary;
}) {
  const t = useTranslations("chat");
  // Power-user setting: hide tier badges (settings toggle, persisted locally).
  const [visible] = useBoolSetting(TIER_BADGE_SETTING, true);
  if (!visible) return null;

  const tierClass = TIER_CLASS[tier] ?? "border-border text-muted-foreground";

  // F2 T16 retokenise: text-[0.65rem] magic → .type-caption (Geist Mono +
  // uppercase + tracking are part of the F1 type-scale utility class).
  if (!routing) {
    return (
      <span
        title={t("tierLabel", { tier })}
        className={cn(CHIP, tierClass)}
        data-slot="tier-badge"
        data-tier={tier}
      >
        {tier}
      </span>
    );
  }

  // Honest reason: the fallback note wins; otherwise the dominant-factor
  // phrase; otherwise (no factor, no fallback) just name the model — never
  // fabricate a factor that wasn't used.
  const why = routing.model_fallback_engaged
    ? t("routing.whyFallback")
    : reasonForFactor(t, routing.dominant_factor);
  const chose = t("routing.chose", { model: shortModel(routing.chosen_model) });

  return (
    <details className="group w-fit" data-slot="tier-badge" data-tier={tier}>
      <summary
        className={cn(
          CHIP,
          tierClass,
          "cursor-pointer list-none gap-1 marker:hidden",
        )}
        aria-label={t("routing.decisionLabel")}
        data-slot="tier-badge-summary"
      >
        <span>{tier}</span>
        <span aria-hidden className="opacity-50">
          ·
        </span>
        <span className="font-mono normal-case" data-slot="tier-badge-model">
          {shortModel(routing.chosen_model)}
        </span>
      </summary>
      <p
        className="type-caption mt-1 max-w-xs text-muted-foreground"
        data-slot="tier-badge-reason"
        data-fallback={routing.model_fallback_engaged}
      >
        {why ? `${chose}. ${why}` : chose}
      </p>
    </details>
  );
}

function reasonForFactor(
  t: ReturnType<typeof useTranslations>,
  factor: RoutingSummary["dominant_factor"],
): string | null {
  switch (factor) {
    case "cost":
      return t("routing.whyCost");
    case "quality":
      return t("routing.whyQuality");
    case "latency":
      return t("routing.whyLatency");
    default:
      return null;
  }
}
