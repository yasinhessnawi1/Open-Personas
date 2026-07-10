"use client";

import { Plug, Trash2 } from "lucide-react";
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

type Server = components["schemas"]["MCPServerDetail"];
type AuthMethod = "none" | "bearer";

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
}: {
  personaId: string;
  /**
   * Spec 35: render the content WITHOUT its own collapsible card — used when
   * nested inside the editor's "Advanced options" section (no card-in-card).
   */
  bare?: boolean;
}) {
  const t = useTranslations("author");
  const { getToken } = useAuth();
  const [servers, setServers] = useState<Server[]>([]);
  const [assigned, setAssigned] = useState<Set<string>>(new Set());
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState<AuthMethod>("none");
  const [credential, setCredential] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState(false);
  const [tests, setTests] = useState<
    Record<string, components["schemas"]["MCPServerTestResult"] | "pending">
  >({});

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
            credential: auth === "bearer" ? credential : null,
          },
        }),
      );
      setName("");
      setUrl("");
      setCredential("");
      setAuth("none");
      await reload();
    } catch {
      setAddError(true);
    } finally {
      setAdding(false);
    }
  }, [adding, name, url, auth, credential, client, reload]);

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
