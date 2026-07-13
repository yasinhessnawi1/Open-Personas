"use client";

import { Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { Stack } from "@/components/layout";
import { Button } from "@/components/ui/button";
import {
  CONNECTOR_CATALOGUE,
  type ConnectorCategory,
  type ConnectorMeta,
} from "@/lib/connectors/catalogue";
import { useConnectorReturn } from "@/lib/connectors/use-connector-return";
import {
  type ConnectorConnection,
  useConnectors,
} from "@/lib/connectors/use-connectors";
import { useDisconnect } from "@/lib/connectors/use-disconnect";
import { cn } from "@/lib/utils";
import { ConnectFlow } from "./connect-flow";
import { ConnectorCard } from "./connector-card";

type Filter = "all" | "connected" | ConnectorCategory;

const CATEGORY_ORDER: readonly ConnectorCategory[] = ["messaging", "email"];

/**
 * Spec C6 (T4) → R11-B4 — the connectors surface in the kit register
 * (`connectors.html`): search + filter chips over the registry, category
 * sections (Fraunces head + honest count), brand-tiled cards. Registry-driven:
 * a new platform is one catalogue entry; grouping, search and the flows follow.
 *
 * The list stays the flows' completion oracle (C6-D-1): `refresh()` re-syncs
 * after a connect/disconnect — state is the source of truth, never an
 * optimistic chip.
 */
export function ConnectorsManager() {
  const t = useTranslations("connectors");
  const { connections, loading, error, refresh } = useConnectors();
  // Handle the OAuth 302-return (T7): refresh the list (sole oracle) + an honest ephemeral
  // toast + strip ?result from the URL. Page-level, zero modal state — works on a cold load.
  useConnectorReturn(refresh);
  const disconnect = useDisconnect(refresh);
  // The platform whose ConnectFlow is open (T5). Mounted per-open so the machine resets each
  // time; `null` closes it.
  const [connecting, setConnecting] = useState<ConnectorMeta | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  // First active binding per platform (the list is newest-first). v1 shows one connection
  // per platform even though the data model permits several.
  const byPlatform = useMemo(() => {
    const map = new Map<string, ConnectorConnection>();
    for (const c of connections) {
      if (!map.has(c.platform)) map.set(c.platform, c);
    }
    return map;
  }, [connections]);

  const initialLoading = loading && connections.length === 0 && error === null;
  const noneConnected = !loading && error === null && connections.length === 0;

  // First-connection guidance (C6-D-5) invites ONLY the platforms whose backend is wired
  // today (the wired-capability rule, T10) — so a first-timer isn't pointed at Discord/Slack
  // while their OAuth issue routes are unmounted (C6-KL-2). Natural-language "A, B, or C".
  const readyList = useMemo(() => {
    const names = CONNECTOR_CATALOGUE.filter((m) => m.backendReady).map((m) =>
      t(`platform.${m.key}`),
    );
    if (names.length <= 1) return names.join("");
    return `${names.slice(0, -1).join(", ")}, ${t("firstConnection.or")} ${names[names.length - 1]}`;
  }, [t]);

  const connectedCount = byPlatform.size;

  // Search matches name + description; chips narrow to connected / a category.
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return CONNECTOR_CATALOGUE.filter((m) => {
      if (filter === "connected" && !byPlatform.has(m.key)) return false;
      if (
        (filter === "messaging" || filter === "email") &&
        m.category !== filter
      )
        return false;
      if (!q) return true;
      return (
        t(`platform.${m.key}`).toLowerCase().includes(q) ||
        t(`desc.${m.key}`).toLowerCase().includes(q)
      );
    });
  }, [query, filter, byPlatform, t]);

  const chips: { key: Filter; label: string; count?: number }[] = [
    { key: "all", label: t("filter.all"), count: CONNECTOR_CATALOGUE.length },
    { key: "connected", label: t("filter.connected"), count: connectedCount },
    { key: "messaging", label: t("category.messaging") },
    { key: "email", label: t("category.email") },
  ];

  return (
    <Stack gap={5} data-slot="connectors-manager">
      {error ? (
        <div
          role="alert"
          data-slot="connectors-error"
          className="flex items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3"
        >
          <p className="type-ui text-destructive">{t("error.load")}</p>
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            {t("error.retry")}
          </Button>
        </div>
      ) : null}

      {noneConnected ? (
        <div
          data-slot="first-connection-guidance"
          className="rounded-lg border bg-muted/30 px-4 py-3"
        >
          <p className="type-ui font-medium">{t("firstConnection.title")}</p>
          <p className="type-caption text-muted-foreground">
            {t("firstConnection.body", { platforms: readyList })}
          </p>
        </div>
      ) : null}

      {/* kit toolbar: search + filter chips over the registry */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-56 flex-1">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("searchPlaceholder")}
            className="h-10 w-full rounded-full border border-border bg-background pl-9 pr-4 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <fieldset
          className="m-0 flex flex-wrap gap-1.5 border-0 p-0"
          aria-label={t("filter.label")}
        >
          {chips.map((chip) => (
            <button
              key={chip.key}
              type="button"
              aria-pressed={filter === chip.key}
              onClick={() => setFilter(chip.key)}
              className={cn(
                "flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground",
                filter === chip.key &&
                  "border-primary/50 bg-primary/10 font-medium text-foreground",
              )}
            >
              {chip.label}
              {chip.count !== undefined ? (
                <span className="text-xs tabular-nums text-muted-foreground">
                  {chip.count}
                </span>
              ) : null}
            </button>
          ))}
        </fieldset>
      </div>

      {visible.length === 0 ? (
        <p className="type-body py-6 text-center text-muted-foreground">
          {t("noMatches")}
        </p>
      ) : (
        CATEGORY_ORDER.map((category) => {
          const items = visible.filter((m) => m.category === category);
          if (items.length === 0) return null;
          return (
            <section key={category} className="flex flex-col gap-3">
              <div className="flex items-baseline gap-2.5">
                <h3 className="font-heading text-lg font-medium tracking-tight">
                  {t(`category.${category}`)}
                </h3>
                <span className="rounded-full border border-border/60 bg-muted px-1.5 py-px text-[11px] tabular-nums text-muted-foreground">
                  {items.length}
                </span>
                <span
                  aria-hidden="true"
                  className="h-px flex-1 self-center bg-border/60"
                />
              </div>
              <div className="grid gap-3 lg:grid-cols-2">
                {items.map((meta) => (
                  <ConnectorCard
                    key={meta.key}
                    meta={meta}
                    connection={byPlatform.get(meta.key) ?? null}
                    loading={initialLoading}
                    onConnect={setConnecting}
                    onDisconnect={(connection) =>
                      void disconnect(meta, connection)
                    }
                  />
                ))}
              </div>
            </section>
          );
        })
      )}

      {connecting ? (
        <ConnectFlow
          meta={connecting}
          connected={byPlatform.has(connecting.key)}
          refresh={refresh}
          onClose={() => setConnecting(null)}
        />
      ) : null}
    </Stack>
  );
}
