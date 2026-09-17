"use client";

import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

const CHIP =
  "type-caption inline-flex w-fit items-center rounded border border-border px-1.5 py-0.5 text-muted-foreground";

/**
 * Spec C0: the persona started this message itself, without a prompt from the user
 * (an agentic run's conclusion today). Sits in the same foot as the tier chip so a
 * message that spoke first is marked wherever it is read: live, or reopened later
 * from the persisted `originated` row.
 */
export function OriginatedBadge({ personaName }: { personaName: string }) {
  const t = useTranslations("chat");
  return (
    <span
      title={t("originatedLabel", { name: personaName })}
      className={cn(CHIP)}
      data-slot="originated-badge"
    >
      {t("originatedBadge")}
    </span>
  );
}
