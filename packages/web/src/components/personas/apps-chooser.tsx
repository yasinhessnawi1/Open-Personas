"use client";

import {
  Boxes,
  Check,
  LinkIcon,
  ShieldCheck,
  ShieldQuestion,
  Wrench,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";
import { useAuth } from "@/auth";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { ApiError, createApiClient, unwrap } from "@/lib/api/client";
import { type AppPresentation, presentApp } from "@/lib/apps/app-labels";
import { cn } from "@/lib/utils";
import { AppSetupForm } from "./app-setup-form";
import { type AppState, deriveAppState, isAppEnabled } from "./app-state";
import { CAPABILITY_SCROLL_LIST_CLASS } from "./capability-list";
import { McpConnectionBadge } from "./mcp-connection-badge";
import type { McpConnectionStatus } from "./mcp-connection-label";
import {
  MCP_CAPABILITIES_OFF,
  type McpCatalogEntry,
  type McpDeploymentCapabilities,
} from "./persona-form";

const MCP_PREFIX = "mcp:";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * N3 (MCP-as-apps) Task 4 + R4 T5 correction — the unified "Apps & Tools" menu.
 *
 * ONE searchable menu, ONE search box, ONE list holding BOTH sources together:
 *   - built-in TOOLS (Web search, Read a file, Run code, Calculator, …), rendered
 *     as friendly app cards (label + one-line what-it-does from the shared
 *     `app-labels` map). Enablement is the bare tool name in the `tools:` list.
 *   - MCP APPS (time, filesystem, github, …), the built-in catalog reframed as
 *     apps (N3-D-5). Enablement stays the `mcp:<name>` tools-list mechanism,
 *     derived through `deriveAppState` / `isAppEnabled`.
 * A single query filters across both. (This replaces the earlier two-stacked-
 * menus layout — each with its own search — that read as two menus.)
 *
 * Honesty discipline (MCP cards):
 *   - N3-D-6: the icon is a LOCAL glyph (lucide + initials) — never a raw
 *     `<img>` to `icon_url` (an arbitrary host: IP/referrer leak + CSP widening,
 *     no `remotePatterns` configured). `icon_url` is a deferred opt-in.
 *   - N3-D-7: the capability relation is ONE honest line (apps.capability) — no
 *     tool-name list, no count (the catalog carries neither — verified gap (c)).
 *   - N3-D-8: a compact trust signal (risk / signed) on the card, the full
 *     provenance disclosure (image / source / allow-hosts) in the detail —
 *     legible-not-opaque while the card stays friendly.
 *
 * See-then-grant holds for both card kinds: the enable button lives inside the
 * expanded detail, so the app/tool is SEEN before it is granted.
 */
export function AppsChooser({
  apps,
  tools = [],
  declaredTools,
  unavailableMcpServers = [],
  connections = [],
  capabilities = MCP_CAPABILITIES_OFF,
  personaId,
  onChange,
}: {
  apps: McpCatalogEntry[];
  /** Built-in tool ids from `GET /v1/tools`, folded into the same menu (R4 T5). */
  tools?: string[];
  declaredTools: string[];
  /** PersonaDetail.unavailable_mcp_servers — empty on surfaces without it. */
  unavailableMcpServers?: string[];
  /**
   * N6 merge-back — per-assigned-server connection status
   * (`GET /personas/{id}/mcp-connections`). An MCP app card with a status shows
   * the friendly badge ("Connected" / "Starting…" / "Needs setup" / …) next to
   * its Enabled/Available state, so "assigned" reads distinctly from "working"
   * (R4-C1-21). Empty (author/new flow, older api, fetch failure) → no badges.
   */
  connections?: McpConnectionStatus[];
  /**
   * Spec N7 (D-N7-2) — which MCP mechanisms this deployment can run. Drives the
   * adopt gate for image-type (`serverType === "server"`) apps + the honest
   * toggle-suppression when nothing on this deployment could ever serve one.
   * Defaults all-off (pre-N7 behavior) for callers that don't thread it.
   */
  capabilities?: McpDeploymentCapabilities;
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

  // Built-in tools resolved to friendly labels once (the shared app-labels map).
  const toolItems = useMemo(
    () => tools.map((name) => ({ name, ...presentApp(name, t) }) as ToolItem),
    [tools, t],
  );

  // server_name → connection status, for the per-card badge lookup.
  const connectionByServer = useMemo(
    () => new Map(connections.map((c) => [c.server_name, c])),
    [connections],
  );

  const q = query.trim().toLowerCase();
  const filteredTools = useMemo(
    () =>
      q
        ? toolItems.filter((i) =>
            `${i.label} ${i.description ?? ""}`.toLowerCase().includes(q),
          )
        : toolItems,
    [toolItems, q],
  );
  const filteredApps = useMemo(
    () =>
      q
        ? apps.filter((a) =>
            `${a.displayName} ${a.name} ${a.description}`
              .toLowerCase()
              .includes(q),
          )
        : apps,
    [apps, q],
  );

  if (tools.length === 0 && apps.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("empty")}</p>;
  }

  function toggleApp(app: McpCatalogEntry) {
    const entry = `${MCP_PREFIX}${app.name}`;
    onChange(
      isAppEnabled(app.name, declaredTools)
        ? declaredTools.filter((x) => x !== entry)
        : [...declaredTools, entry],
    );
  }

  function toggleTool(name: string) {
    onChange(
      declaredTools.includes(name)
        ? declaredTools.filter((x) => x !== name)
        : [...declaredTools, name],
    );
  }

  const nothingMatches =
    filteredTools.length === 0 && filteredApps.length === 0;

  return (
    <div className="flex flex-col gap-3" data-slot="apps-chooser">
      <Input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("searchCombined")}
        aria-label={t("searchCombined")}
        data-slot="apps-search"
      />
      {nothingMatches ? (
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
          {/* Built-in tools first, then connected MCP apps — ONE list, ONE search. */}
          {filteredTools.map((item) => (
            <li key={`tool:${item.name}`}>
              <ToolCard
                item={item}
                enabled={declaredTools.includes(item.name)}
                onToggle={() => toggleTool(item.name)}
              />
            </li>
          ))}
          {filteredApps.map((app) => (
            <li key={`app:${app.name}`}>
              <AppCard
                app={app}
                state={deriveAppState(
                  app,
                  declaredTools,
                  unavailableMcpServers,
                )}
                connection={connectionByServer.get(app.name)}
                capabilities={capabilities}
                personaId={personaId}
                onToggle={() => toggleApp(app)}
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
  connection,
  capabilities = MCP_CAPABILITIES_OFF,
  personaId,
  onToggle,
}: {
  app: McpCatalogEntry;
  state: AppState;
  /** N6 merge-back — this server's connection status; absent → no badge. */
  connection?: McpConnectionStatus;
  /** Spec N7 (D-N7-2) — deployment capabilities; defaults all-off. */
  capabilities?: McpDeploymentCapabilities;
  personaId?: string;
  onToggle: () => void;
}) {
  const t = useTranslations("apps");
  const title = app.displayName || app.name;
  const enabled = state === "enabled";
  const unavailable = state === "unavailable";
  const isImageApp = app.serverType === "server";
  // Spec N4 (Group D) + N7 (D-N7-2, the image-app adoption gate): a remote app
  // that declares a credential is self-adoptable (unchanged from N4); an
  // IMAGE-type app is ALSO self-adoptable once this deployment has a per-tenant
  // runtime to actually spawn it on — regardless of whether it declares a secret
  // (a secretless image app still needs the adopt→assign door, never the
  // allow-list toggle, since T1's grant expansion deliberately excludes image
  // servers; the credential form itself accommodates zero secrets, N7 owner
  // ruling). Requires an existing persona to adopt against (edit flow only).
  const adoptable =
    !!personaId &&
    ((app.serverType === "remote" && app.secrets.length > 0) ||
      (isImageApp && capabilities.perTenantRuntime));
  // Any image app that is NOT going through the adopt form — whether because
  // there is no persona yet to adopt against (the author/new flow) or this
  // deployment has no per-tenant runtime to spawn it on — suppresses the
  // allow-list toggle rather than leaving it inert: it would write a
  // `mcp:<name>` grant no mechanism can ever serve (image tools only ever
  // arrive via adoption + assignment; T1's grant expansion deliberately
  // excludes image servers). The server-side C-filter already excludes the
  // no-mechanism-at-all case from the catalog entirely, so on an existing
  // persona this is reached only when an operator gateway is configured (the
  // app stays listed — the catalog can't see what's enabled there).
  const imageAppUnservable = isImageApp && !adoptable;

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
            {/* N6 merge-back: "assigned" reads distinctly from "working" —
                the runtime's per-server connection status as a friendly badge
                (R4-C1-21). Absent status (author flow, fetch failure) → nothing. */}
            {connection ? (
              <McpConnectionBadge
                connected={connection.connected}
                reason={connection.reason}
              />
            ) : null}
            <CardTrustSignal app={app} />
          </span>
        </CollapsibleTrigger>

        <CollapsibleContent>
          <div className="flex flex-col gap-3 px-3 pt-3" data-slot="app-detail">
            {/* R9-039: the catalog's REAL per-server description leads the
                detail, untruncated — this is the card's identity now, not the
                generic boilerplate that used to open it. */}
            <p
              className="text-sm text-foreground"
              data-slot="app-full-description"
            >
              {app.description || t("detail.noDescription")}
            </p>

            {/* N3-D-7: ONE honest capability line — no enumerated tools/count
                (the catalog carries neither for any server — verified gap).
                R9-039: labeled + demoted beneath the real description above,
                not the card's lead line. */}
            <div data-slot="app-capability-section">
              <p
                className="text-xs font-medium text-foreground/70"
                data-slot="app-capability-heading"
              >
                {t("toolsHeading")}
              </p>
              <p
                className="text-xs text-muted-foreground"
                data-slot="app-capability"
              >
                {t("capability")}
              </p>
            </div>

            {/* N3-D-8: full trust disclosure, legible-not-opaque. R9-039:
                compact labeled rows + a real link, boilerplate as a footnote. */}
            <TrustDisclosure app={app} />

            {/* States / enablement. */}
            {unavailable ? (
              <p
                className="text-sm text-destructive"
                data-slot="app-unavailable"
              >
                {t("unavailable.summary")}
              </p>
            ) : app.authMethod === "oauth" && personaId ? (
              // N7-T3b (D-N7-3): an oauth catalog app (e.g. github) is granted by
              // Connect, not a credential form or a bare toggle — adopt(no secret)
              // then hand off to the provider's authorize flow.
              <AppOauthConnect app={app} personaId={personaId} />
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
                {/* N7 (D-N7-2): an image app with no mechanism to run it on THIS
                    deployment suppresses the toggle — it can only ever write an
                    inert grant (see `imageAppUnservable` above). */}
                {imageAppUnservable ? null : (
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
                )}
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

/**
 * N3-D-8: full disclosure in the detail — what this app IS.
 *
 * R9-039: restructured from a stack of generic prose sentences (the same
 * "An app is a real integration…" line opening every card) into compact
 * labeled rows carrying the catalog's REAL per-server provenance — the image
 * it runs, a REAL clickable link to its source, and its egress allow-list.
 * The honest "what an app is" framing stays (N3 safety invariant), but as a
 * short footnote at the end, not the section's identity.
 */
function TrustDisclosure({ app }: { app: McpCatalogEntry }) {
  const t = useTranslations("apps");
  return (
    <dl
      className="flex flex-col gap-1.5 text-xs text-muted-foreground"
      data-slot="app-trust"
    >
      {app.image ? (
        <div className="flex gap-1.5" data-slot="app-trust-row">
          <dt className="shrink-0 font-medium text-foreground/70">
            {t("trust.runsLabel")}
          </dt>
          <dd className="truncate font-mono">{app.image}</dd>
        </div>
      ) : null}
      {app.sourceProject ? (
        <div className="flex gap-1.5" data-slot="app-trust-row">
          <dt className="shrink-0 font-medium text-foreground/70">
            {t("trust.sourceLabel")}
          </dt>
          <dd className="truncate">
            <a
              href={app.sourceProject}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-foreground"
            >
              {app.sourceProject}
            </a>
            {app.sourceCommit ? ` @ ${app.sourceCommit.slice(0, 12)}` : null}
          </dd>
        </div>
      ) : null}
      <div className="flex gap-1.5" data-slot="app-trust-row">
        <dt className="shrink-0 font-medium text-foreground/70">
          {t("trust.hostsLabel")}
        </dt>
        <dd className="truncate">
          {app.allowHosts.length > 0
            ? app.allowHosts.join(", ")
            : t("trust.allowHostsNone")}
        </dd>
      </div>
      {/* The generic safety framing — now a footnote, not the card's identity. */}
      <p
        className="pt-0.5 text-xs text-muted-foreground/70"
        data-slot="app-trust-footnote"
      >
        {t("trust.honest")}
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

/**
 * N7-T3b (D-N7-3) — the catalog-card Connect affordance for an OAuth app.
 *
 * Replaces the setup form / toggle for an `authMethod === "oauth"` catalog entry.
 * On click: adopt the app with NO credential (the oauth token arrives via the
 * dance, never here), tolerating a 409 if it was already adopted; resolve the
 * adopted server's id by its `catalog_source`; then POST authorize and hand the
 * browser to the provider. After the callback page completes the exchange the user
 * lands back here (redirect_after = the current edit path) and the connection badge
 * + `has_credential` reflect the connected state.
 *
 * see-then-grant: rendered INSIDE the expanded detail, after the trust disclosure.
 */
function AppOauthConnect({
  app,
  personaId,
}: {
  app: McpCatalogEntry;
  personaId: string;
}) {
  const t = useTranslations("apps");
  const { getToken } = useAuth();
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connect = useCallback(async () => {
    if (connecting) return;
    setConnecting(true);
    setError(null);
    try {
      const jwt = await getToken(TEMPLATE ? { template: TEMPLATE } : undefined);
      const api = createApiClient(() => Promise.resolve(jwt));
      // 1. Adopt with no secret. 409 = already adopted → fall through to Connect.
      try {
        await unwrap(
          await api.POST("/v1/personas/{persona_id}/adopted-apps", {
            params: { path: { persona_id: personaId } },
            body: { catalog_name: app.name, credential: null },
          }),
        );
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e;
      }
      // 2. Resolve the adopted server's id by its catalog provenance.
      const servers = await unwrap(await api.GET("/v1/mcp-servers"));
      const server = servers.find((s) => s.catalog_source === app.name);
      if (!server) throw new Error("adopted server not found");
      // 3. Begin the OAuth dance; hand the browser to the provider.
      const res = await unwrap(
        await api.POST("/v1/mcp-servers/{server_id}/oauth/authorize", {
          params: { path: { server_id: server.id } },
          body: {
            redirect_after:
              typeof window !== "undefined"
                ? window.location.pathname + window.location.search
                : null,
          },
        }),
      );
      window.location.assign(res.authorize_url);
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setError(
        status === 403 ? t("setupForm.errorNotVetted") : t("connect.error"),
      );
      setConnecting(false);
    }
  }, [connecting, getToken, personaId, app.name, t]);

  return (
    <div className="flex flex-col gap-2" data-slot="app-oauth-connect">
      <p className="text-xs text-muted-foreground">{t("connect.intro")}</p>
      <button
        type="button"
        onClick={() => void connect()}
        disabled={connecting}
        className={cn(buttonVariants({ size: "sm" }), "w-fit gap-1.5")}
        data-slot="app-connect"
      >
        <LinkIcon className="size-3.5" aria-hidden="true" />
        {connecting ? t("connect.connecting") : t("connect.connect")}
      </button>
      {error ? (
        <p className="text-xs text-destructive" data-slot="app-connect-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** A built-in tool resolved to its friendly presentation (R4 T5). */
interface ToolItem extends AppPresentation {
  readonly name: string;
}

/**
 * R4 T5 — a built-in tool rendered as an app card in the unified Apps & Tools
 * list. A simple, always-available ability (no trust/setup/adoption): its icon
 * is the wrench glyph and its enable button lives INSIDE the expanded detail, so
 * the tool is SEEN (label + one-line what-it-does) before it is granted —
 * consistent with the MCP `AppCard` see-then-grant grammar.
 */
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
              is expanded, so the tool is SEEN before it is granted. */}
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
