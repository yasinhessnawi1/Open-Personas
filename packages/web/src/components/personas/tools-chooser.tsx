"use client";

import { Check, Wrench } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { type AppPresentation, presentApp } from "@/lib/apps/app-labels";
import { cn } from "@/lib/utils";
import { CAPABILITY_SCROLL_LIST_CLASS } from "./capability-list";

/**
 * R4 T5 — the built-in tools, reframed as "Apps".
 *
 * The old raw `ChipToggle` showed lowercase ids (`datetime`, `web_search`) and
 * was a quick-toggle that enabled a capability on click, BEFORE the user saw
 * what it was — a violation of the see-then-grant grammar the apps/specialities
 * menus already honour. This renders each built-in tool as a friendly app card
 * (label + one-line what-it-does) whose enable button lives INSIDE the expanded
 * detail, so you see the app before you grant it. Enablement stays the plain
 * tool-name entry in the persona's `tools:` list (bare name, not `mcp:`), the
 * same YAML path the chip used.
 */
export function ToolsChooser({
  tools,
  declaredTools,
  empty,
  onChange,
}: {
  /** Built-in tool ids from `GET /v1/tools`. */
  tools: string[];
  /** The persona's current `tools:` list (bare names + `mcp:` entries). */
  declaredTools: string[];
  /** Copy shown when the deployment exposes no built-in tools. */
  empty: string;
  onChange: (tools: string[]) => void;
}) {
  const t = useTranslations("apps");
  const [query, setQuery] = useState("");

  const items = useMemo(
    () => tools.map((name) => ({ name, ...presentApp(name, t) }) as ToolItem),
    [tools, t],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) =>
      `${item.label} ${item.description ?? ""}`.toLowerCase().includes(q),
    );
  }, [items, query]);

  if (tools.length === 0) {
    return <p className="text-sm text-muted-foreground">{empty}</p>;
  }

  function toggle(name: string) {
    onChange(
      declaredTools.includes(name)
        ? declaredTools.filter((x) => x !== name)
        : [...declaredTools, name],
    );
  }

  return (
    <div className="flex flex-col gap-3" data-slot="tools-chooser">
      <Input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("searchPlaceholder")}
        aria-label={t("searchPlaceholder")}
        data-slot="tools-search"
      />
      {filtered.length === 0 ? (
        <p
          className="text-sm text-muted-foreground"
          data-slot="tools-search-empty"
        >
          {t("searchEmpty", { query: query.trim() })}
        </p>
      ) : (
        <ul
          className={cn("flex flex-col gap-2", CAPABILITY_SCROLL_LIST_CLASS)}
          data-slot="tools-list"
        >
          {filtered.map((item) => (
            <li key={item.name}>
              <ToolCard
                item={item}
                enabled={declaredTools.includes(item.name)}
                onToggle={() => toggle(item.name)}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface ToolItem extends AppPresentation {
  readonly name: string;
}

function ToolCard({
  item,
  enabled,
  onToggle,
}: {
  item: ToolItem;
  enabled: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations("apps");
  return (
    <Card
      size="sm"
      data-slot="tool-card"
      data-state={enabled ? "enabled" : "available"}
    >
      <Collapsible>
        <CollapsibleTrigger
          className="flex w-full items-center gap-3 px-3 text-left"
          aria-label={t("open", { name: item.label })}
        >
          <span
            aria-hidden="true"
            data-slot="tool-icon"
            className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 text-primary"
          >
            <Wrench className="size-4" />
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-heading text-sm font-semibold">
              {item.label}
            </span>
            {item.description ? (
              <span className="truncate text-xs text-muted-foreground">
                {item.description}
              </span>
            ) : null}
          </span>
          <span className="ml-auto shrink-0">
            <Badge
              variant={enabled ? "default" : "outline"}
              data-slot="tool-state-badge"
            >
              {enabled ? t("state.enabled") : t("state.available")}
            </Badge>
          </span>
        </CollapsibleTrigger>

        <CollapsibleContent>
          {/* See-then-grant: the enable button is reachable only after the card
              is expanded, so the app is SEEN before it is granted. */}
          <div
            className="flex flex-col gap-3 px-3 pt-3"
            data-slot="tool-detail"
          >
            {item.description ? (
              <p className="text-sm text-muted-foreground">
                {item.description}
              </p>
            ) : null}
            <button
              type="button"
              onClick={onToggle}
              aria-pressed={enabled}
              data-slot="tool-toggle"
              className={cn(
                "inline-flex w-fit items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors",
                enabled
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:border-primary/30",
              )}
            >
              {enabled ? (
                <Check className="size-3.5" aria-hidden="true" />
              ) : null}
              {enabled ? t("enable.disable") : t("enable.enable")}
            </button>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}
