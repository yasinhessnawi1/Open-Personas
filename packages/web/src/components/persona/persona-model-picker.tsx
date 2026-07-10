"use client";

import { Check, ChevronDown, Cpu } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { ModelOption } from "@/lib/api/models-client";
import { cn } from "@/lib/utils";

/**
 * Spec M1 (T7) — reusable per-persona model picker with price tags.
 *
 * Presentation-agnostic (the R9-014 PersonaPicker discipline): it renders
 * whatever `models` it's handed — it never fetches, never knows what "chose a
 * model" means beyond reporting the id back through `onSelect`. `null` always
 * means "use the tier default" (routes by task, T1/T3's existing behaviour);
 * the caller (the persona editor's Model section) owns fetching the catalog
 * (`models-client.ts`, self-fetched — T5's route) and wiring the persisted
 * choice into `routing.preferred_model` (T1, additive-optional).
 *
 * Fail-open by construction: an empty `models` list still renders "Use tier
 * default" — there is no error state, matching T5's own fail-open contract.
 *
 * The default view shows only the curated shortlist (`recommended: true`) —
 * "Browse all" (a menu item with `closeOnClick={false}`, so it doesn't close
 * the menu) reveals the rest of whatever list the caller passed in. The
 * currently-selected model is always shown even if it isn't recommended (a
 * persona pinned to a since-de-listed model must still read as selected).
 */
export interface PersonaModelPickerProps {
  models: readonly ModelOption[];
  value: string | null;
  onSelect: (id: string | null) => void;
  className?: string;
}

function formatUsd(perMillionTokens: number): string {
  return `$${perMillionTokens.toFixed(2)}`;
}

export function PersonaModelPicker({
  models,
  value,
  onSelect,
  className,
}: PersonaModelPickerProps) {
  const t = useTranslations("modelPicker");
  const [browseAll, setBrowseAll] = useState(false);

  const selected =
    value !== null ? (models.find((m) => m.id === value) ?? null) : null;
  const selectedLabel = selected ? selected.label : t("tierDefault");
  const hasMore =
    !browseAll && models.some((m) => !m.recommended && m.id !== value);
  const visible = browseAll
    ? models
    : models.filter((m) => m.recommended || m.id === value);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={`${t("title")}: ${selectedLabel}`}
        data-slot="model-picker-trigger"
        className={cn(
          buttonVariants({ variant: "outline" }),
          "w-full max-w-sm justify-between gap-2",
          className,
        )}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <Cpu className="size-4 shrink-0" aria-hidden="true" />
          <span className="truncate">{selectedLabel}</span>
        </span>
        <ChevronDown
          className="size-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="max-h-80 min-w-72 overflow-y-auto"
        data-slot="model-picker-content"
      >
        <DropdownMenuItem
          onClick={() => onSelect(null)}
          data-slot="model-picker-tier-default"
          className="gap-2"
        >
          <Check
            className={cn("size-3.5 shrink-0", value !== null && "opacity-0")}
            aria-hidden="true"
          />
          <span className="truncate">{t("tierDefault")}</span>
        </DropdownMenuItem>

        {models.length === 0 ? (
          <p
            className="px-2 py-3 text-center text-sm text-muted-foreground"
            data-slot="model-picker-empty"
          >
            {t("empty")}
          </p>
        ) : (
          <>
            <DropdownMenuSeparator />
            {visible.map((m) => (
              <DropdownMenuItem
                key={m.id}
                onClick={() => onSelect(m.id)}
                data-slot="model-picker-item"
                className="items-start gap-2"
              >
                <Check
                  className={cn(
                    "mt-0.5 size-3.5 shrink-0",
                    m.id !== value && "opacity-0",
                  )}
                  aria-hidden="true"
                />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate text-sm">{m.label}</span>
                    {m.recommended ? (
                      <Badge
                        variant="secondary"
                        data-slot="model-picker-recommended-badge"
                      >
                        {t("recommended")}
                      </Badge>
                    ) : null}
                  </span>
                  <span
                    className="type-caption text-muted-foreground"
                    data-slot="model-picker-price"
                  >
                    {t("pricePerM", {
                      in: formatUsd(m.input_price_per_1m),
                      out: formatUsd(m.output_price_per_1m),
                    })}
                  </span>
                </span>
              </DropdownMenuItem>
            ))}
            {hasMore ? (
              <DropdownMenuItem
                closeOnClick={false}
                onClick={() => setBrowseAll(true)}
                data-slot="model-picker-browse-all"
              >
                {t("browseAll")}
              </DropdownMenuItem>
            ) : null}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
