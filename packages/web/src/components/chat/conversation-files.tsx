"use client";

import {
  Code2,
  Eye,
  FileCode,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileText,
  FileType,
  FolderOpen,
  Workflow,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { type ComponentType, useEffect, useMemo, useState } from "react";
import { useAuth } from "@/auth";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import {
  type ArtifactItem,
  CONVERSATION_FILES_CHANGED_EVENT,
  useConversationArtifacts,
} from "@/lib/hooks/use-conversation-artifacts";
import { cn } from "@/lib/utils";
import { CHAT_STREAMING_EVENT } from "./chat-presence-orb";
import { ArtifactView } from "./renderers";
import {
  isBinaryKind,
  type RendererKind,
  rendererKindFor,
} from "./renderers/types";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

type LucideIcon = ComponentType<{
  className?: string;
  "aria-hidden"?: boolean;
}>;

const ICON_BY_KIND: Record<RendererKind, LucideIcon> = {
  markdown: FileText,
  code: FileCode,
  plaintext: FileText,
  json: FileJson,
  csv: FileSpreadsheet,
  html: FileCode,
  pdf: FileType,
  image: FileImage,
  mermaid: Workflow,
  graphviz: Workflow,
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** User-facing filename: the original upload name, else the path's last segment. */
function nameOf(item: ArtifactItem): string {
  return item.metadata?.original_name ?? item.ref.split("/").pop() ?? item.ref;
}

/**
 * Short stable token to disambiguate two files that share a display name (two
 * different uploads both named `report.pdf`, or two generated artifacts with the
 * same suggested name). Derived from the ref's basename: the upload's `doc_ref`
 * uuid tail (`report-a1b2c3d4` → `a1b2c3d4`) or the content hash's head.
 */
function refToken(item: ArtifactItem): string {
  const base = (item.ref.split("/").pop() ?? item.ref).replace(/\.[^.]+$/, "");
  return base.includes("-")
    ? base.slice(base.lastIndexOf("-") + 1)
    : base.slice(0, 8);
}

/**
 * Build a ref→label map: the clean `nameOf` for unique names, suffixed with a
 * short `refToken` only where a name collides — so identical-looking files stay
 * distinguishable without cluttering the common (unique) case. `nameOf` stays
 * the canonical name for downloads + renderer-kind detection.
 */
function labelMap(items: ArtifactItem[]): Map<string, string> {
  const counts = new Map<string, number>();
  for (const it of items)
    counts.set(nameOf(it), (counts.get(nameOf(it)) ?? 0) + 1);
  const labels = new Map<string, string>();
  for (const it of items) {
    const name = nameOf(it);
    labels.set(
      it.ref,
      (counts.get(name) ?? 0) > 1 ? `${name} · ${refToken(it)}` : name,
    );
  }
  return labels;
}

type Mode = "rendered" | "raw";

/**
 * Spec 35 — the conversation Files viewer (split layout, per the v1 design's
 * header affordance next to Call). One wide Sheet: a left rail listing the
 * conversation's UNIFIED file set — grouped "Shared by you" (uploads) vs
 * "Made by {persona}" (generated) — and a preview pane reusing the Spec-28
 * <ArtifactView> (every file kind: image/chart/doc/code/pdf/diagram). The
 * unified list is one Spec-F5 call (useConversationArtifacts); no merge needed.
 */
export function ConversationFiles({
  personaId,
  conversationId,
  personaName,
  open: openProp,
  onOpenChange,
}: {
  personaId: string;
  conversationId: string;
  personaName: string;
  /**
   * R9-024: controlled-open escape hatch so a sibling right-panel (the new chat
   * Calendar) can close this one on open — "only one right panel at a time".
   * Omitted (every existing call site, including this component's own tests),
   * the panel keeps its uncontrolled internal `open` state — byte-identical.
   */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const t = useTranslations("chat.files");
  const tr = useTranslations("chat.output.renderer");
  const { getToken } = useAuth();
  const { items, refresh } = useConversationArtifacts(
    personaId,
    conversationId,
  );

  const [openState, setOpenState] = useState(false);
  const open = openProp ?? openState;
  const setOpen = onOpenChange ?? setOpenState;
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("rendered");

  // Keep the registry in sync (the list / badge must not go stale):
  //   · opening the viewer always re-fetches;
  //   · a finished turn may have produced a persona artifact (streaming→false);
  //   · a user upload fires CONVERSATION_FILES_CHANGED.
  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  useEffect(() => {
    const onStreaming = (e: Event) => {
      // Refresh once the turn settles — the persona may have written a file.
      if ((e as CustomEvent<boolean>).detail === false) void refresh();
    };
    const onChanged = () => void refresh();
    window.addEventListener(CHAT_STREAMING_EVENT, onStreaming);
    window.addEventListener(CONVERSATION_FILES_CHANGED_EVENT, onChanged);
    return () => {
      window.removeEventListener(CHAT_STREAMING_EVENT, onStreaming);
      window.removeEventListener(CONVERSATION_FILES_CHANGED_EVENT, onChanged);
    };
  }, [refresh]);

  // Two-group split by provenance. Anything without source metadata is treated
  // as persona-made (the generated path is the common metadata-less case).
  const { uploads, generated } = useMemo(() => {
    const uploads: ArtifactItem[] = [];
    const generated: ArtifactItem[] = [];
    for (const it of items) {
      if (it.metadata?.source === "upload") uploads.push(it);
      else generated.push(it);
    }
    return { uploads, generated };
  }, [items]);

  // Collision-aware display labels (clean name unless it collides → suffix).
  const labels = useMemo(() => labelMap(items), [items]);
  const labelOf = (item: ArtifactItem) => labels.get(item.ref) ?? nameOf(item);

  // Default the preview to the first file once the list lands / the panel opens.
  useEffect(() => {
    if (open && selectedRef === null && items.length > 0) {
      setSelectedRef(items[0].ref);
    }
  }, [open, selectedRef, items]);

  const selected = items.find((i) => i.ref === selectedRef) ?? null;
  const selectedKind = selected
    ? rendererKindFor(selected.media_type, nameOf(selected))
    : "plaintext";
  const selectedBinary = isBinaryKind(selectedKind);

  async function download(item: ArtifactItem): Promise<void> {
    let objectUrl: string | null = null;
    try {
      const token = await getToken(
        TEMPLATE ? { template: TEMPLATE } : undefined,
      );
      const res = await fetch(
        `${API}/v1/personas/${encodeURIComponent(personaId)}/uploads/${item.ref}`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {} },
      );
      if (!res.ok) throw new Error(`download ${res.status}`);
      const blob = await res.blob();
      objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = nameOf(item);
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch {
      // Best-effort download; no toast surface in this header affordance.
    } finally {
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t("button")}
        title={t("button")}
        className={cn(
          "relative grid size-9 shrink-0 place-items-center rounded-md border border-border text-muted-foreground",
          "hover:bg-muted hover:text-foreground",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
        )}
        data-slot="conversation-files-button"
      >
        <FolderOpen className="size-4" aria-hidden />
        {items.length > 0 ? (
          <span
            className="type-caption absolute -top-1.5 -right-1.5 grid min-w-4 place-items-center rounded-full bg-primary px-1 font-medium text-primary-foreground tabular-nums"
            data-slot="conversation-files-count"
          >
            {items.length}
          </span>
        ) : null}
      </button>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          side="right"
          showCloseButton
          className="w-full gap-0 p-0 sm:w-[60vw] sm:!max-w-5xl"
          data-slot="conversation-files-viewer"
        >
          <header className="flex items-center gap-2 border-b border-border px-4 py-3">
            <FolderOpen
              className="size-4 shrink-0 text-muted-foreground"
              aria-hidden
            />
            <SheetTitle className="type-ui min-w-0 flex-1 truncate">
              {t("title")}
            </SheetTitle>
          </header>

          <div className="flex min-h-0 flex-1">
            {/* Left rail — the unified, provenance-grouped file list. */}
            <nav
              className="w-56 shrink-0 overflow-y-auto border-r border-border py-2"
              aria-label={t("title")}
              data-slot="conversation-files-rail"
            >
              {items.length === 0 ? (
                <div className="px-4 py-6">
                  <p className="type-ui text-muted-foreground">{t("empty")}</p>
                  <p className="type-caption mt-1 text-muted-foreground">
                    {t("emptyHint", { name: personaName })}
                  </p>
                </div>
              ) : (
                <>
                  <FileGroup
                    label={t("uploaded")}
                    items={uploads}
                    selectedRef={selectedRef}
                    onSelect={setSelectedRef}
                    labelOf={labelOf}
                  />
                  <FileGroup
                    label={t("generated", { name: personaName })}
                    items={generated}
                    selectedRef={selectedRef}
                    onSelect={setSelectedRef}
                    labelOf={labelOf}
                  />
                </>
              )}
            </nav>

            {/* Preview pane — reuses the Spec-28 renderer for every file kind. */}
            <div className="flex min-w-0 flex-1 flex-col">
              {selected ? (
                <>
                  <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                    <span
                      className="type-ui min-w-0 flex-1 truncate"
                      title={labelOf(selected)}
                    >
                      {labelOf(selected)}
                    </span>
                    {!selectedBinary ? (
                      <div className="flex items-center rounded-md border border-border">
                        <ToolbarButton
                          active={mode === "rendered"}
                          label={tr("showRendered")}
                          onClick={() => setMode("rendered")}
                        >
                          <Eye className="size-4" aria-hidden />
                        </ToolbarButton>
                        <ToolbarButton
                          active={mode === "raw"}
                          label={tr("showRaw")}
                          onClick={() => setMode("raw")}
                        >
                          <Code2 className="size-4" aria-hidden />
                        </ToolbarButton>
                      </div>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => void download(selected)}
                      className="type-caption rounded-md border border-border px-2 py-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                      data-slot="conversation-files-download"
                    >
                      {t("downloadShort")}
                    </button>
                  </div>
                  <div className="min-h-0 flex-1 overflow-auto">
                    <ArtifactView
                      key={`${selected.ref}:${mode}`}
                      kind={selectedKind}
                      mode={mode}
                      personaId={personaId}
                      workspacePath={selected.ref}
                      mediaType={selected.media_type}
                    />
                  </div>
                </>
              ) : (
                <div className="grid flex-1 place-items-center p-6">
                  <p className="type-ui text-muted-foreground">
                    {t("selectPrompt")}
                  </p>
                </div>
              )}
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}

function FileGroup({
  label,
  items,
  selectedRef,
  onSelect,
  labelOf,
}: {
  label: string;
  items: ArtifactItem[];
  selectedRef: string | null;
  onSelect: (ref: string) => void;
  labelOf: (item: ArtifactItem) => string;
}) {
  if (items.length === 0) return null;
  return (
    <div className="mb-1">
      <p className="type-caption px-4 py-1.5 font-medium text-muted-foreground uppercase">
        {label}
      </p>
      <ul>
        {items.map((item) => {
          const name = nameOf(item);
          const display = labelOf(item);
          const kind = rendererKindFor(item.media_type, name);
          const Icon = ICON_BY_KIND[kind];
          const active = item.ref === selectedRef;
          return (
            <li key={item.ref}>
              <button
                type="button"
                onClick={() => onSelect(item.ref)}
                className={cn(
                  "flex w-full items-center gap-2 px-4 py-1.5 text-left",
                  "hover:bg-muted/60",
                  active && "bg-muted",
                )}
                aria-current={active ? "true" : undefined}
                data-slot="conversation-files-row"
              >
                <Icon
                  className="size-4 shrink-0 text-muted-foreground"
                  aria-hidden
                />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="type-ui truncate" title={display}>
                    {display}
                  </span>
                  <span className="type-caption text-muted-foreground">
                    {kind.toUpperCase()} · {formatSize(item.size_bytes)}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function ToolbarButton({
  children,
  label,
  active,
  onClick,
}: {
  children: React.ReactNode;
  label: string;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      aria-pressed={active}
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground",
        "hover:bg-muted hover:text-foreground",
        active && "bg-muted text-foreground",
      )}
    >
      {children}
    </button>
  );
}
