"use client";

import { Copy, MessageSquare, Mic, Phone, Play, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useState } from "react";
import { startChat, startVoice } from "@/app/actions";
import {
  NewTaskDialog,
  type NewTaskPersona,
} from "@/components/activity/new-task-dialog";
import type { ArtifactListResponse } from "@/components/artifacts/artifact-gallery";
import { ArtifactGallery } from "@/components/artifacts/artifact-gallery";
import { EpisodicManagerModal } from "@/components/memory/episodic-manager-modal";
import { AvatarModal } from "@/components/persona/avatar-modal";
import { PersonaMemoriesModal } from "@/components/persona/persona-memories-modal";
import type { McpConnectionStatus } from "@/components/personas/mcp-connection-label";
import { PersonaEditor } from "@/components/personas/persona-editor";
import {
  MCP_CAPABILITIES_OFF,
  type McpCatalogEntry,
  type McpDeploymentCapabilities,
} from "@/components/personas/persona-form";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { Button, buttonVariants } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useApi } from "@/lib/api/use-api";
import { renameInIdentity } from "@/lib/persona";
import {
  savePersonaInline,
  setConsent as setConsentAction,
} from "@/lib/persona-actions";
import { type PersonaDoc, readIdentity } from "@/lib/persona-draft";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { cn } from "@/lib/utils";

type SaveStatus = "saved" | "saving" | "error";

/**
 * R11-B6 (owner-ruled consolidation) — THE persona page: detail + edit + the
 * post-authoring preview are ONE inline-editable surface (the kit's
 * `persona-detail.html`, copied whole). Everything edits in place and saves on
 * its own (debounced PATCH through the same door the old Save button used);
 * the sticky bar reads Saving… ⇄ All changes saved ⇄ the honest error. The
 * old `/personas/[id]/edit` route redirects here; every "Edit" button died
 * with it.
 *
 * Layout = the kit: hero (avatar + live name/role + Message/Call/Run-a-task),
 * the editor column (typed-memory stores, constraints, voice, capabilities,
 * model, autonomy+consent, advanced incl. raw YAML + BYO-MCP), and the right
 * rail (at-a-glance, quick actions, danger zone).
 */
export function PersonaPage({
  personaId,
  initialDoc,
  tools,
  skills,
  mcpServers,
  mcpConnections,
  mcpCapabilities = MCP_CAPABILITIES_OFF,
  initialConsent,
  initialAvatarUrl,
  conversationCount,
  tasksRunCount,
  memoryCount,
  createdAt,
  initialArtifacts,
  newTaskAction,
}: {
  personaId: string;
  initialDoc: PersonaDoc;
  tools: string[];
  skills: string[];
  mcpServers: McpCatalogEntry[];
  mcpConnections: McpConnectionStatus[];
  // Spec N7 (D-N7-2) — which MCP mechanisms this deployment can run, threaded to
  // the editor's apps chooser. Optional, defaults all-off (pre-N7 behavior).
  mcpCapabilities?: McpDeploymentCapabilities;
  initialConsent: boolean | null;
  initialAvatarUrl: string | null;
  conversationCount: number;
  tasksRunCount: number;
  memoryCount: number;
  createdAt: string | null;
  initialArtifacts: ArtifactListResponse;
  newTaskAction: (formData: FormData) => void | Promise<void>;
}) {
  const t = useTranslations("personaPage");
  // next-intl's formatter is pinned to the active locale on both server + client,
  // so the rendered date matches (a bare `toLocaleDateString()` used the runtime
  // default locale, which differs SSR↔browser → hydration mismatch, R9-044).
  const format = useFormatter();
  const router = useRouter();
  const api = useApi();
  const confirm = useConfirm();
  const { notify } = useNotify();

  const [identity, setIdentity] = useState(() => readIdentity(initialDoc));
  const [avatarUrl, setAvatarUrl] = useState<string | null>(initialAvatarUrl);
  const [status, setStatus] = useState<SaveStatus>("saved");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);

  const onDocChange = useCallback((doc: PersonaDoc) => {
    setIdentity(readIdentity(doc));
  }, []);

  const onSaveStatus = useCallback((s: SaveStatus, error?: string) => {
    setStatus(s);
    setSaveError(error ?? null);
  }, []);

  const dialogPersona: NewTaskPersona = {
    id: personaId,
    name: identity.name,
    avatar_url: avatarUrl,
  };

  async function duplicate() {
    const ok = await confirm({
      title: t("duplicateTitle", { name: identity.name }),
      description: t("duplicateBody"),
      confirmLabel: t("duplicate"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      const detail = await api.GET("/v1/personas/{persona_id}", {
        params: { path: { persona_id: personaId } },
      });
      const yaml = detail.data?.yaml;
      if (!yaml) throw new Error("no yaml");
      const created = await api.POST("/v1/personas", {
        body: {
          yaml: renameInIdentity(yaml, `${identity.name} (copy)`),
          avatar_url: null,
        },
      });
      const newId = created.data?.id;
      if (newId) {
        notify({
          level: "info",
          title: t("duplicated", { name: identity.name }),
        });
        router.push(`/personas/${newId}`);
      } else throw new Error("create failed");
    } catch {
      notify({ level: "error", title: t("duplicateFailed") });
    } finally {
      setBusy(false);
    }
  }

  async function destroy() {
    const ok = await confirm({
      title: t("deleteTitle", { name: identity.name }),
      description: t("deleteBody", { name: identity.name }),
      confirmLabel: t("delete"),
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api.DELETE("/v1/personas/{persona_id}", {
        params: { path: { persona_id: personaId } },
      });
      router.push("/personas");
    } catch {
      notify({ level: "error", title: t("deleteFailed") });
      setBusy(false);
    }
  }

  return (
    <div
      className="mx-auto w-full max-w-6xl px-4 pb-16 sm:px-6"
      style={personaIdentityStyle({ id: personaId })}
      data-slot="persona-page"
    >
      {/* sticky bar: back + the always-visible autosave status (kit) */}
      <div className="sticky top-0 z-10 -mx-4 flex items-center gap-3 border-border border-b bg-background/90 px-4 py-2.5 backdrop-blur sm:-mx-6 sm:px-6">
        <Link
          href="/personas"
          className="text-sm text-muted-foreground hover:text-foreground"
        >
          ‹ {t("back")}
        </Link>
        <span
          className={cn(
            "type-caption normal-case tracking-normal ml-auto flex items-center gap-1.5",
            status === "error" ? "text-destructive" : "text-muted-foreground",
          )}
          data-slot="save-status"
          data-status={status}
        >
          <span
            aria-hidden="true"
            className={cn(
              "size-1.5 rounded-full",
              status === "saved" && "bg-emerald-500",
              status === "saving" && "bg-amber-500",
              status === "error" && "bg-destructive",
            )}
          />
          {status === "saving"
            ? t("saving")
            : status === "error"
              ? (saveError ?? t("saveFailed"))
              : t("saved")}
        </span>
      </div>

      {/* hero: avatar + live name/role + the three doors (kit) */}
      <header className="flex flex-wrap items-start gap-5 py-6">
        <AvatarModal
          personaId={personaId}
          name={identity.name}
          avatarUrl={avatarUrl}
          onChange={setAvatarUrl}
        />
        <div className="min-w-0 flex-1">
          <h1 className="truncate font-heading text-3xl font-semibold tracking-tight">
            {identity.name || t("unnamed")}
          </h1>
          <p className="mt-0.5 truncate text-muted-foreground">
            {identity.role}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <form action={startChat.bind(null, personaId)}>
              <Button type="submit" size="sm" className="gap-1.5">
                <MessageSquare className="size-4" aria-hidden="true" />
                {t("message")}
              </Button>
            </form>
            <form action={startVoice.bind(null, personaId)}>
              <Button
                type="submit"
                variant="outline"
                size="sm"
                className="gap-1.5"
              >
                <Phone className="size-4" aria-hidden="true" />
                {t("call")}
              </Button>
            </form>
            <NewTaskDialog
              personas={[dialogPersona]}
              action={newTaskAction}
              trigger={
                <button
                  type="button"
                  className={cn(
                    buttonVariants({ variant: "outline", size: "sm" }),
                    "gap-1.5",
                  )}
                >
                  <Play className="size-4" aria-hidden="true" />
                  {t("runTask")}
                </button>
              }
            />
          </div>
        </div>
      </header>

      <div className="gap-6 lg:grid lg:grid-cols-[minmax(0,1fr)_280px]">
        {/* main column: the full editor, autosaving in place */}
        <PersonaEditor
          initialDoc={initialDoc}
          tools={tools}
          skills={skills}
          mcpServers={mcpServers}
          mcpConnections={mcpConnections}
          mcpCapabilities={mcpCapabilities}
          personaId={personaId}
          onSave={(yaml, avatar) => savePersonaInline(personaId, yaml, avatar)}
          saveLabel=""
          autosave
          nav={false}
          sectionsOpen
          hideAvatar
          avatarUrlOverride={avatarUrl}
          onDocChange={onDocChange}
          onSaveStatus={onSaveStatus}
          initialConsent={initialConsent}
          onConsentChange={(granted) => setConsentAction(personaId, granted)}
        />

        {/* right rail (kit): at-a-glance · quick actions · danger zone */}
        <aside className="mt-6 flex flex-col gap-4 lg:sticky lg:top-14 lg:mt-0 lg:max-h-[calc(100vh-4.5rem)] lg:self-start lg:overflow-y-auto">
          <section className="rounded-xl border border-border bg-card p-4">
            <h3 className="type-caption mb-3 text-muted-foreground">
              {t("glance")}
            </h3>
            <dl className="flex flex-col gap-2 text-sm">
              {/* D-K11-7: the whole row is the trigger, not just the count —
                  opens the persona-scoped episodic manager. */}
              <EpisodicManagerModal
                personas={[dialogPersona]}
                trigger={
                  <button
                    type="button"
                    className="-mx-1 flex items-center justify-between gap-2 rounded-md px-1 py-0.5 text-left hover:bg-muted/60"
                  >
                    <span className="text-muted-foreground">
                      {t("conversations")}
                    </span>
                    <span className="tabular-nums">{conversationCount}</span>
                  </button>
                }
              />
              {/* D-K11-7 rider: whole-row-clickable (previously only the
                  number opened the graph modal) — the modal itself unchanged. */}
              <PersonaMemoriesModal
                personaId={personaId}
                count={memoryCount}
                trigger={
                  <button
                    type="button"
                    className="-mx-1 flex items-center justify-between gap-2 rounded-md px-1 py-0.5 text-left hover:bg-muted/60"
                  >
                    <span className="text-muted-foreground">
                      {t("memories")}
                    </span>
                    <span className="tabular-nums underline decoration-dotted underline-offset-2">
                      {memoryCount}
                    </span>
                  </button>
                }
              />
              <div className="flex justify-between">
                <dt className="text-muted-foreground">{t("tasksRun")}</dt>
                <dd className="tabular-nums">{tasksRunCount}</dd>
              </div>
              {createdAt ? (
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">{t("created")}</dt>
                  <dd>
                    {format.dateTime(new Date(createdAt), {
                      dateStyle: "medium",
                    })}
                  </dd>
                </div>
              ) : null}
            </dl>
          </section>

          <section className="rounded-xl border border-border bg-card p-4">
            <h3 className="type-caption mb-3 text-muted-foreground">
              {t("quickActions")}
            </h3>
            <div className="flex flex-col gap-2">
              <form action={startChat.bind(null, personaId)}>
                <Button type="submit" className="w-full gap-1.5">
                  <MessageSquare className="size-4" aria-hidden="true" />
                  {t("messageName", { name: identity.name })}
                </Button>
              </form>
              <form action={startVoice.bind(null, personaId)}>
                <Button
                  type="submit"
                  variant="outline"
                  className="w-full gap-1.5"
                >
                  <Mic className="size-4" aria-hidden="true" />
                  {t("voiceCall")}
                </Button>
              </form>
              <Button
                type="button"
                variant="outline"
                className="w-full gap-1.5"
                disabled={busy}
                onClick={() => void duplicate()}
              >
                <Copy className="size-4" aria-hidden="true" />
                {t("duplicate")}
              </Button>
              <Button
                type="button"
                variant="ghost"
                className="w-full gap-1.5"
                onClick={() => setFilesOpen(true)}
              >
                {t("files")}
              </Button>
            </div>
          </section>

          <section className="rounded-xl border border-destructive/30 bg-destructive/5 p-4">
            <h3 className="type-caption mb-2 text-destructive">
              {t("dangerZone")}
            </h3>
            <p className="mb-3 text-sm text-muted-foreground">
              {t("deleteHint", { name: identity.name })}
            </p>
            <Button
              type="button"
              variant="outline"
              className="w-full gap-1.5 border-destructive/40 text-destructive hover:bg-destructive/10"
              disabled={busy}
              onClick={() => void destroy()}
            >
              <Trash2 className="size-4" aria-hidden="true" />
              {t("deletePersona")}
            </Button>
          </section>
        </aside>
      </div>

      {/* R11-B6 rider (owner-ruled): files are an OVERLAY, not a route. */}
      <Sheet open={filesOpen} onOpenChange={setFilesOpen}>
        <SheetContent
          side="right"
          showCloseButton
          className="w-full gap-0 overflow-y-auto p-0 sm:w-[560px] sm:max-w-xl"
          data-slot="persona-files-panel"
        >
          <header className="border-border border-b px-4 py-3">
            <SheetTitle className="font-heading text-base font-semibold tracking-tight">
              {t("filesTitle", { name: identity.name })}
            </SheetTitle>
          </header>
          <div className="min-h-0 flex-1 px-4 py-3">
            {initialArtifacts.total === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                {t("filesEmpty")}
              </p>
            ) : (
              <ArtifactGallery
                personaId={personaId}
                initial={initialArtifacts}
              />
            )}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
