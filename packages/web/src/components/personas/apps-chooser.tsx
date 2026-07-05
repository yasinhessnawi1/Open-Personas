"use client";

import { Boxes, Check, ShieldCheck, ShieldQuestion } from "lucide-react";
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
import { cn } from "@/lib/utils";
import { AppSetupForm } from "./app-setup-form";
import { type AppState, deriveAppState, isAppEnabled } from "./app-state";
import { CAPABILITY_SCROLL_LIST_CLASS } from "./capability-list";
import type { McpCatalogEntry } from "./persona-form";

const MCP_PREFIX = "mcp:";

/**
 * N3 (MCP-as-apps) Task 4 — the apps chooser.
 *
 * Reframes the built-in MCP catalog as an "apps" experience (N3-D-5): a
 * searchable directory of app cards, each expanding to a per-app detail view.
 * Decoupled from the raw tools view. Per-persona enablement stays the
 * `mcp:<name>` tools-list mechanism (reframed presentation of the old McpToggle),
 * derived through Task 3's `deriveAppState` / `isAppEnabled` (never re-derived
 * inline) so the one-colon enablement rule has one home.
 *
 * Honesty discipline:
 *   - N3-D-6: the icon is a LOCAL glyph (lucide + initials) — never a raw
 *     `<img>` to `icon_url` (an arbitrary host: IP/referrer leak + CSP widening,
 *     no `remotePatterns` configured). `icon_url` is a deferred opt-in.
 *   - N3-D-7: the capability relation is ONE honest line (apps.capability) — no
 *     tool-name list, no count (the catalog carries neither — verified gap (c)).
 *   - N3-D-8: a compact trust signal (risk / signed) on the card, the full
 *     provenance disclosure (image / source / allow-hosts) in the detail —
 *     legible-not-opaque while the card stays friendly.
 */
export function AppsChooser({
  apps,
  declaredTools,
  unavailableMcpServers = [],
  personaId,
  onChange,
}: {
  apps: McpCatalogEntry[];
  declaredTools: string[];
  /** PersonaDetail.unavailable_mcp_servers — empty on surfaces without it. */
  unavailableMcpServers?: string[];
  /**
   * Spec N4 (Group D) — the persona being edited. Present in the edit flow only;
   * absent in author/new (no id to adopt against). When present, a remote app that
   * declares a credential renders the credential-isolated setup form (adoption);
   * absent → the read-honest needs-setup disclosure (the N3 behavior).
   */
  personaId?: string;
  onChange: (tools: string[]) => void;
}) {
  const t = useTranslations("apps");
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return apps;
    return apps.filter((a) => {
      const haystack =
        `${a.displayName} ${a.name} ${a.description}`.toLowerCase();
      return haystack.includes(q);
    });
  }, [apps, query]);

  if (apps.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("empty")}</p>;
  }

  function toggle(app: McpCatalogEntry) {
    const entry = `${MCP_PREFIX}${app.name}`;
    onChange(
      isAppEnabled(app.name, declaredTools)
        ? declaredTools.filter((x) => x !== entry)
        : [...declaredTools, entry],
    );
  }

  return (
    <div className="flex flex-col gap-3" data-slot="apps-chooser">
      <Input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("searchPlaceholder")}
        aria-label={t("searchPlaceholder")}
        data-slot="apps-search"
      />
      {filtered.length === 0 ? (
        <p
          className="text-sm text-muted-foreground"
          data-slot="apps-search-empty"
        >
          {t("searchEmpty", { query: query.trim() })}
        </p>
      ) : (
        <ul
          className={cn("flex flex-col gap-2", CAPABILITY_SCROLL_LIST_CLASS)}
          data-slot="apps-list"
        >
          {filtered.map((app) => (
            <li key={app.name}>
              <AppCard
                app={app}
                state={deriveAppState(
                  app,
                  declaredTools,
                  unavailableMcpServers,
                )}
                personaId={personaId}
                onToggle={() => toggle(app)}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Two-letter initials for the local glyph fallback (mirrors persona-card). */
function appInitials(label: string): string {
  const cleaned = label.trim();
  if (!cleaned) return "?";
  const parts = cleaned.split(/[\s_-]+/).filter(Boolean);
  const letters =
    (parts.length > 1 ? parts[0][0] + parts[1][0] : cleaned.slice(0, 2)) ?? "?";
  return letters.toUpperCase();
}

function AppCard({
  app,
  state,
  personaId,
  onToggle,
}: {
  app: McpCatalogEntry;
  state: AppState;
  personaId?: string;
  onToggle: () => void;
}) {
  const t = useTranslations("apps");
  const title = app.displayName || app.name;
  const enabled = state === "enabled";
  const unavailable = state === "unavailable";
  // Spec N4 (Group D): a remote app that declares a credential is self-adoptable —
  // the user supplies the secret via the setup form (N4-D-1/N4-D-10). Requires an
  // existing persona to adopt against (edit flow only). Local-container / no-credential
  // apps keep the read-honest disclosure + the allow-list toggle (the N3 behavior).
  const adoptable =
    !!personaId && app.serverType === "remote" && app.secrets.length > 0;

  return (
    <Card size="sm" data-slot="app-card" data-state={state}>
      <Collapsible>
        <CollapsibleTrigger
          className="flex w-full items-center gap-3 px-3 text-left"
          aria-label={t("open", { name: title })}
        >
          {/* N3-D-6: LOCAL glyph only — never <img src={icon_url}>. */}
          <span
            aria-hidden="true"
            data-slot="app-icon"
            className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 font-heading text-xs font-medium text-primary"
          >
            {app.iconUrl ? appInitials(title) : <Boxes className="size-4" />}
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-heading text-sm font-semibold">
              {title}
            </span>
            <span className="truncate text-xs text-muted-foreground">
              {app.description}
            </span>
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-1.5">
            <StateBadge state={state} />
            <CardTrustSignal app={app} />
          </span>
        </CollapsibleTrigger>

        <CollapsibleContent>
          <div className="flex flex-col gap-3 px-3 pt-3" data-slot="app-detail">
            {/* N3-D-7: ONE honest capability line — no enumerated tools/count. */}
            <p
              className="text-sm text-muted-foreground"
              data-slot="app-capability"
            >
              {t("capability")}
            </p>

            {/* N3-D-8: full trust disclosure, legible-not-opaque. */}
            <TrustDisclosure app={app} />

            {/* States / enablement. */}
            {unavailable ? (
              <p
                className="text-sm text-destructive"
                data-slot="app-unavailable"
              >
                {t("unavailable.summary")}
              </p>
            ) : adoptable && personaId ? (
              // N4: the credential-isolated setup form IS the grant for an adoptable
              // remote app — the user supplies the secret, it posts straight to the
              // store, and adoption assigns the app to the persona.
              <AppSetupForm app={app} personaId={personaId} />
            ) : (
              <>
                {app.requiredEnv.length > 0 || app.secrets.length > 0 ? (
                  <NeedsSetupNote app={app} />
                ) : null}
                <button
                  type="button"
                  onClick={onToggle}
                  aria-pressed={enabled}
                  data-slot="app-toggle"
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
              </>
            )}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

function StateBadge({ state }: { state: AppState }) {
  const t = useTranslations("apps");
  const variant =
    state === "unavailable"
      ? "destructive"
      : state === "enabled"
        ? "default"
        : "outline";
  const label =
    state === "needs-setup"
      ? t("state.needsSetup")
      : state === "enabled"
        ? t("state.enabled")
        : state === "unavailable"
          ? t("state.unavailable")
          : t("state.available");
  return (
    <Badge variant={variant} data-slot="app-state-badge">
      {label}
    </Badge>
  );
}

/** N3-D-8: compact card signal — signed mark + coarse risk only. */
function CardTrustSignal({ app }: { app: McpCatalogEntry }) {
  const t = useTranslations("apps");
  return (
    <span className="flex items-center gap-1" data-slot="app-trust-signal">
      {app.signed ? (
        <ShieldCheck
          className="size-3.5 text-muted-foreground"
          aria-label={t("trust.signed")}
        />
      ) : (
        <ShieldQuestion
          className="size-3.5 text-muted-foreground"
          aria-label={t("trust.unsigned")}
        />
      )}
      {app.risk && app.risk !== "low" ? (
        <Badge variant="outline" data-slot="app-risk">
          {t("trust.riskLabel", { risk: app.risk })}
        </Badge>
      ) : null}
    </span>
  );
}

/** N3-D-8: full disclosure in the detail — what this app IS. */
function TrustDisclosure({ app }: { app: McpCatalogEntry }) {
  const t = useTranslations("apps");
  return (
    <dl
      className="flex flex-col gap-1 text-xs text-muted-foreground"
      data-slot="app-trust"
    >
      <p>{t("trust.honest")}</p>
      {app.image ? <p>{t("trust.image", { image: app.image })}</p> : null}
      {app.sourceProject ? (
        <p>
          {app.sourceCommit
            ? t("trust.sourceCommit", {
                project: app.sourceProject,
                commit: app.sourceCommit.slice(0, 12),
              })
            : t("trust.source", { project: app.sourceProject })}
        </p>
      ) : null}
      <p>
        {app.allowHosts.length > 0
          ? t("trust.allowHosts", { hosts: app.allowHosts.join(", ") })
          : t("trust.allowHostsNone")}
      </p>
    </dl>
  );
}

/**
 * N3-D-10: read-honest needs-setup disclosure — informational, NOT a form, NOT
 * a disabled field. Names WHO sets it ("deployment level"); the app DECLARES a
 * requirement (N3 has no read-back of whether the operator set it).
 */
function NeedsSetupNote({ app }: { app: McpCatalogEntry }) {
  const t = useTranslations("apps");
  // Prefer the richer secrets[] env names; fall back to required_env.
  const envs =
    app.secrets.length > 0 ? app.secrets.map((s) => s.env) : app.requiredEnv;
  return (
    <div
      className="flex flex-col gap-1 rounded-md border border-border bg-muted/40 p-2 text-xs text-muted-foreground"
      data-slot="app-needs-setup"
    >
      <p className="font-medium text-foreground">{t("needsSetup.heading")}</p>
      <p>{t("needsSetup.summary")}</p>
      {envs.map((env) => (
        <p key={env}>{t("needsSetup.credentialNeedsLabel", { env })}</p>
      ))}
    </div>
  );
}
