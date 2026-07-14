"use client";

import { LinkIcon, Plug, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/auth";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createApiClient, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { cn } from "@/lib/utils";
import { CollapsibleSection } from "./collapsible-section";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

// Spec N7-T3b: the generic `mcp-native` OAuth path needs NO operator config — it
// discovers the server's authorization server (RFC 9728/8414) + DCR-registers on
// the fly — so it is ALWAYS offered as a provider choice, alongside any operator-
// configured named providers (fail-closed mirror of the API provider registry).
const MCP_NATIVE_PROVIDER = "mcp-native";

type Server = components["schemas"]["MCPServerDetail"];
type AuthMethod = "none" | "bearer" | "oauth";

/**
 * Spec 30 T12 — bring-your-own MCP management + per-persona assignment.
 *
 * Lists the user's own MCP servers, adds new ones (URL + optional bearer token —
 * SSRF-validated + encrypted server-side), tests + discovers their tools, and
 * assigns/unassigns them to THIS persona. Credentials are entered here but never
 * returned (the row shows only `has_credential`). Rendered only for an existing
 * persona (it needs a persona id to assign to).
 */
export function ByoMcpManager({
  personaId,
  bare = false,
  oauthProviders = [],
}: {
  personaId: string;
  /**
   * Spec 35: render the content WITHOUT its own collapsible card — used when
   * nested inside the editor's "Advanced options" section (no card-in-card).
   */
  bare?: boolean;
  /**
   * Spec N7-T3b: the operator-configured OAuth providers (from the deployment
   * capabilities block, `capabilities.oauth_providers`). Fail-closed — a named
   * provider only appears when its client is configured server-side. The generic
   * `mcp-native` auto-discover option is offered regardless (it needs no config).
   */
  oauthProviders?: string[];
}) {
  const t = useTranslations("author");
  const { getToken } = useAuth();
  const [servers, setServers] = useState<Server[]>([]);
  const [assigned, setAssigned] = useState<Set<string>>(new Set());
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState<AuthMethod>("none");
  const [credential, setCredential] = useState("");
  const [oauthProvider, setOauthProvider] = useState(MCP_NATIVE_PROVIDER);
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState(false);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [tests, setTests] = useState<
    Record<string, components["schemas"]["MCPServerTestResult"] | "pending">
  >({});

  // The provider select: every operator-configured named provider (fail-closed —
  // absent when unconfigured) PLUS the always-available generic auto-discover path.
  const providerOptions = [...oauthProviders, MCP_NATIVE_PROVIDER];

  const client = useCallback(async () => {
    const jwt = await getToken(TEMPLATE ? { template: TEMPLATE } : undefined);
    return createApiClient(() => Promise.resolve(jwt));
  }, [getToken]);

  const reload = useCallback(async () => {
    const api = await client();
    // Fire both GETs concurrently (no `await` before Promise.all sees them) —
    // awaiting each request before building the array would run them
    // sequentially and could race an early rejection as unhandled.
    const [all, mine] = await Promise.all([
      api.GET("/v1/mcp-servers").then(unwrap),
      api
        .GET("/v1/personas/{persona_id}/mcp-servers", {
          params: { path: { persona_id: personaId } },
        })
        .then(unwrap),
    ]);
    setServers(all);
    setAssigned(new Set(mine.map((s) => s.id)));
  }, [client, personaId]);

  useEffect(() => {
    void reload().catch(() => {});
  }, [reload]);

  const add = useCallback(async () => {
    if (adding || !name.trim() || !url.trim()) return;
    setAdding(true);
    setAddError(false);
    try {
      const api = await client();
      await unwrap(
        await api.POST("/v1/mcp-servers", {
          body: {
            name,
            url,
            auth_method: auth,
            // oauth carries NO credential at create — the per-user token is
            // obtained later via Connect (POST oauth/authorize → callback).
            credential: auth === "bearer" ? credential : null,
            oauth_provider: auth === "oauth" ? oauthProvider : null,
          },
        }),
      );
      setName("");
      setUrl("");
      setCredential("");
      setAuth("none");
      setOauthProvider(MCP_NATIVE_PROVIDER);
      await reload();
    } catch {
      setAddError(true);
    } finally {
      setAdding(false);
    }
  }, [adding, name, url, auth, credential, oauthProvider, client, reload]);

  // Spec N7-T3b (R8-D-8): begin (or renew) the OAuth dance for an oauth server.
  // POST authorize → the API mints server-side state + PKCE and returns the
  // provider authorize URL; we hand the browser off to it. `redirect_after` brings
  // the user back to the editor after the callback page completes the exchange.
  const connect = useCallback(
    async (id: string) => {
      if (connecting) return;
      setConnecting(id);
      try {
        const api = await client();
        const res = await unwrap(
          await api.POST("/v1/mcp-servers/{server_id}/oauth/authorize", {
            params: { path: { server_id: id } },
            body: {
              redirect_after:
                typeof window !== "undefined"
                  ? window.location.pathname + window.location.search
                  : null,
            },
          }),
        );
        window.location.assign(res.authorize_url);
      } catch {
        setConnecting(null);
      }
    },
    [connecting, client],
  );

  const test = useCallback(
    async (id: string) => {
      setTests((m) => ({ ...m, [id]: "pending" }));
      try {
        const api = await client();
        const res = await unwrap(
          await api.POST("/v1/mcp-servers/{server_id}/test", {
            params: { path: { server_id: id } },
          }),
        );
        setTests((m) => ({ ...m, [id]: res }));
        await reload();
      } catch {
        setTests((m) => ({
          ...m,
          [id]: { ok: false, tools: [], error: "error" },
        }));
      }
    },
    [client, reload],
  );

  const remove = useCallback(
    async (id: string) => {
      const api = await client();
      await api.DELETE("/v1/mcp-servers/{server_id}", {
        params: { path: { server_id: id } },
      });
      await reload();
    },
    [client, reload],
  );

  const toggleAssign = useCallback(
    async (id: string, on: boolean) => {
      const api = await client();
      const path = { persona_id: personaId, server_id: id };
      if (on) {
        await api.DELETE("/v1/personas/{persona_id}/mcp-servers/{server_id}", {
          params: { path },
        });
      } else {
        await api.PUT("/v1/personas/{persona_id}/mcp-servers/{server_id}", {
          params: { path },
        });
      }
      await reload();
    },
    [client, personaId, reload],
  );

  const body = (
    <>
      <p
        className="type-caption text-muted-foreground"
        data-slot="byo-mcp-manager"
      >
        {t("byoSubtitle")}
      </p>

      {/* Add form */}
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap gap-2">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("byoName")}
            className="w-40"
            aria-label={t("byoName")}
          />
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={t("byoUrl")}
            className="min-w-56 flex-1"
            aria-label={t("byoUrl")}
          />
          <select
            value={auth}
            onChange={(e) => setAuth(e.target.value as AuthMethod)}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm shadow-xs"
            aria-label={t("byoAuth")}
          >
            <option value="none">{t("byoAuthNone")}</option>
            <option value="bearer">{t("byoAuthBearer")}</option>
            <option value="oauth">{t("byoAuthOauth")}</option>
          </select>
          {auth === "bearer" ? (
            <Input
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
              placeholder={t("byoCredential")}
              type="password"
              className="w-44"
              aria-label={t("byoCredential")}
            />
          ) : null}
          {auth === "oauth" ? (
            <select
              value={oauthProvider}
              onChange={(e) => setOauthProvider(e.target.value)}
              className="h-9 rounded-md border border-input bg-transparent px-2 text-sm shadow-xs"
              aria-label={t("byoOauthProvider")}
              data-slot="byo-oauth-provider"
            >
              {providerOptions.map((p) => (
                <option key={p} value={p}>
                  {p === MCP_NATIVE_PROVIDER ? t("byoOauthNative") : p}
                </option>
              ))}
            </select>
          ) : null}
          <button
            type="button"
            onClick={() => void add()}
            disabled={adding || !name.trim() || !url.trim()}
            className={cn(buttonVariants({ size: "sm" }), "gap-1.5")}
          >
            <Plug className="size-3.5" aria-hidden="true" />
            {adding ? t("byoAdding") : t("byoAdd")}
          </button>
        </div>
        {addError ? (
          <p className="text-xs text-destructive">{t("byoAddError")}</p>
        ) : null}
      </div>

      {/* Server list */}
      {servers.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("byoEmpty")}</p>
      ) : (
        <ul className="flex flex-col gap-2" data-slot="byo-server-list">
          {servers.map((s) => {
            const isOn = assigned.has(s.id);
            const result = tests[s.id];
            return (
              <li
                key={s.id}
                className="flex flex-col gap-1 rounded-md border p-2"
                data-slot="byo-server"
                data-assigned={isOn}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs">{s.name}</span>
                  <span className="truncate text-xs text-muted-foreground">
                    {s.url}
                  </span>
                  {!s.enabled ? (
                    <span className="type-caption rounded-sm bg-muted px-1">
                      {t("byoDisabled")}
                    </span>
                  ) : null}
                  <div className="ml-auto flex items-center gap-1.5">
                    {/* N7-T3b (R8-D-8): an oauth server needs a per-user token —
                        Connect starts the dance; once connected the row shows
                        Reconnect (re-auth), driven by has_credential. */}
                    {s.auth_method === "oauth" ? (
                      <button
                        type="button"
                        onClick={() => void connect(s.id)}
                        disabled={connecting === s.id}
                        className={cn(
                          buttonVariants({
                            variant: s.has_credential ? "outline" : "default",
                            size: "sm",
                          }),
                          "gap-1.5",
                        )}
                        data-slot="byo-connect"
                        data-connected={s.has_credential}
                      >
                        <LinkIcon className="size-3.5" aria-hidden="true" />
                        {connecting === s.id
                          ? t("byoConnecting")
                          : s.has_credential
                            ? t("byoReconnect")
                            : t("byoConnect")}
                      </button>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => void test(s.id)}
                      className={cn(
                        buttonVariants({ variant: "outline", size: "sm" }),
                      )}
                      data-slot="byo-test"
                    >
                      {result === "pending" ? t("byoTesting") : t("byoTest")}
                    </button>
                    <button
                      type="button"
                      onClick={() => void toggleAssign(s.id, isOn)}
                      aria-pressed={isOn}
                      className={cn(
                        buttonVariants({
                          variant: isOn ? "default" : "outline",
                          size: "sm",
                        }),
                      )}
                      data-slot="byo-assign"
                    >
                      {isOn ? t("byoUnassign") : t("byoAssign")}
                    </button>
                    <button
                      type="button"
                      onClick={() => void remove(s.id)}
                      aria-label={t("byoDelete")}
                      className={cn(
                        buttonVariants({ variant: "ghost", size: "sm" }),
                        "text-muted-foreground hover:text-destructive",
                      )}
                      data-slot="byo-delete"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </div>
                </div>
                {result && result !== "pending" ? (
                  <p
                    className={cn(
                      "type-caption",
                      result.ok ? "text-muted-foreground" : "text-destructive",
                    )}
                    data-slot="byo-test-result"
                  >
                    {result.ok
                      ? t("byoTestOk", { count: result.tools?.length ?? 0 })
                      : t("byoTestFail", { reason: result.error ?? "error" })}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );

  if (bare) {
    return (
      <div className="flex flex-col gap-3" data-slot="byo-mcp-bare">
        <h3 className="font-heading text-sm font-semibold tracking-wide text-foreground uppercase">
          {t("byoTitle")}
        </h3>
        {body}
      </div>
    );
  }
  return (
    <CollapsibleSection id="byo-mcp" title={t("byoTitle")}>
      {body}
    </CollapsibleSection>
  );
}
