"use client";

import { Shield } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  CARE_COLOR,
  kindColor,
  type LinkType,
  linkEncoding,
  type NodeKind,
} from "@/lib/memory/encoding";

/**
 * Spec K5 — the connections + categories legend (K5-D-3), matching the design's
 * dual-label key: each typed link shows its technical term + a plain gloss
 * ("Causal · led to"), and the node-kind Categories key maps each colour swatch
 * to its kind. Colours come from the shared encoding module so the legend stays
 * in step with the canvas. The K4 sensitive mark reads as care (K5-D-10).
 */
const LINK_KEYS: LinkType[] = ["semantic", "entity", "temporal", "causal"];
const KIND_KEYS: NodeKind[] = [
  "concept",
  "fact",
  "preference",
  "trait",
  "goal",
  "circumstance",
  "entity",
];

export function MemoryLegend() {
  const t = useTranslations("memory");
  return (
    <aside
      aria-label={t("legendLabel")}
      className="pointer-events-none absolute bottom-4 right-4 hidden w-56 rounded-xl border bg-card/90 p-3.5 shadow-[var(--elevation-1)] backdrop-blur md:block"
    >
      <h4 className="type-caption mb-2 text-muted-foreground">
        {t("legendConnections")}
      </h4>
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {LINK_KEYS.map((key) => {
          const enc = linkEncoding(key);
          const dotted = key === "semantic";
          return (
            <li key={key} className="flex items-center gap-2.5 text-xs">
              <span
                className="w-6 shrink-0"
                style={{
                  borderTopWidth: `${enc.width + 0.5}px`,
                  borderTopStyle: dotted
                    ? "dotted"
                    : enc.dash.length > 0
                      ? "dashed"
                      : "solid",
                  borderTopColor: enc.color,
                  opacity: dotted ? 0.7 : 1,
                }}
              />
              <span>
                {t(`linkType.${key}`)}{" "}
                <span className="text-muted-foreground">
                  · {t(`linkGloss.${key}`)}
                </span>
              </span>
            </li>
          );
        })}
      </ul>

      <div className="my-2.5 h-px bg-border" />
      <h4 className="type-caption mb-2 text-muted-foreground">
        {t("legendCategories")}
      </h4>
      <ul className="m-0 grid grid-cols-2 gap-x-2 gap-y-1 p-0">
        {KIND_KEYS.map((kind) => (
          <li key={kind} className="flex items-center gap-2 text-xs">
            <span
              className="size-2.5 shrink-0 rounded-full"
              style={{ background: kindColor(kind) }}
            />
            <span className="truncate">{t(`kind.${kind}`)}</span>
          </li>
        ))}
      </ul>

      <div className="my-2.5 h-px bg-border" />
      <div className="flex items-center gap-2 text-xs">
        <Shield className="size-3.5 shrink-0" style={{ color: CARE_COLOR }} />
        <span className="text-muted-foreground">{t("legendSensitive")}</span>
      </div>
    </aside>
  );
}
