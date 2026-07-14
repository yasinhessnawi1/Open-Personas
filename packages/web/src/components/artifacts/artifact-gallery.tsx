"use client";

import { Dialog } from "@base-ui/react/dialog";
import {
  Download,
  File,
  FileText,
  LineChart,
  Table2,
  Trash2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { useAuth } from "@/auth";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { AuthedImage } from "@/components/ui/authed-image";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api/use-api";
import { cn } from "@/lib/utils";

interface ArtifactMetadataView {
  source: string;
  type: string;
  producing_spec: string;
  conversation_id: string | null;
  created_at: string;
  original_name: string | null;
}

export interface ArtifactItem {
  ref: string;
  size_bytes: number;
  media_type: string;
  metadata?: ArtifactMetadataView | null;
}

export interface ArtifactListResponse {
  total: number;
  limit: number;
  offset: number;
  items: readonly ArtifactItem[];
}

export interface ArtifactGalleryProps {
  personaId: string;
  initial: ArtifactListResponse;
}

const SOURCE_CHIPS = ["all", "upload", "generated"] as const;
const TYPE_CHIPS = ["all", "image", "chart", "doc", "data"] as const;

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Friendly display name: the original filename, else the ref's basename. */
function displayName(item: ArtifactItem): string {
  return item.metadata?.original_name ?? item.ref.split("/").pop() ?? item.ref;
}

/** The uppercase extension badge (from the name, else the media subtype). */
function extBadge(item: ArtifactItem): string {
  const name = displayName(item);
  const dot = name.lastIndexOf(".");
  if (dot > 0 && dot < name.length - 1)
    return name.slice(dot + 1).toUpperCase();
  return (item.media_type.split("/")[1] ?? "FILE").toUpperCase().slice(0, 5);
}

function isImageLike(item: ArtifactItem): boolean {
  return item.media_type.startsWith("image/");
}

const TYPE_ICON = {
  doc: FileText,
  data: Table2,
  chart: LineChart,
} as const;

function typeIcon(item: ArtifactItem) {
  const t = item.metadata?.type;
  return t && t in TYPE_ICON ? TYPE_ICON[t as keyof typeof TYPE_ICON] : File;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Spec F5 T14/T15 → R11-B6 rider (owner-ruled redesign) — the persona files
 * gallery, no longer a scaffold: REAL previews (image/chart tiles render the
 * actual picture through the authed blob pipe; docs/data get an elegant type
 * tile with an extension badge), hover-revealed actions, and a PREVIEW dialog
 * (images large; documents as a meta card). Download does the authed
 * fetch→blob dance — the old `/api/...` href pointed at a route that never
 * existed. Delete stays the atomic bytes+sidecar door.
 */
export function ArtifactGallery({ personaId, initial }: ArtifactGalleryProps) {
  const t = useTranslations("artifacts");
  const tc = useTranslations("confirm");
  const tn = useTranslations("notifications");
  const confirm = useConfirm();
  const { notify } = useNotify();
  const { getToken } = useAuth();
  const api = useApi();
  const [deleting, setDeleting] = useState<string | null>(null);
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  const [sourceFilter, setSourceFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [preview, setPreview] = useState<ArtifactItem | null>(null);

  const items = useMemo(() => {
    return initial.items.filter((item) => {
      if (removed.has(item.ref)) return false;
      if (sourceFilter !== "all" && item.metadata?.source !== sourceFilter)
        return false;
      if (typeFilter !== "all" && item.metadata?.type !== typeFilter)
        return false;
      return true;
    });
  }, [initial.items, sourceFilter, typeFilter, removed]);

  async function download(item: ArtifactItem) {
    try {
      const token = await getToken();
      const res = await fetch(
        `${API}/v1/personas/${encodeURIComponent(personaId)}/uploads/${item.ref}`,
        { headers: token ? { Authorization: `Bearer ${token}` } : undefined },
      );
      if (!res.ok) throw new Error(`${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = displayName(item);
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      notify({ level: "error", title: t("downloadFailed") });
    }
  }

  async function handleDelete(item: ArtifactItem) {
    if (deleting) return;
    const name = displayName(item);
    const ok = await confirm({
      title: tc("deleteTitle", { name }),
      description: t("deleteConfirm", { ref: name }),
      confirmLabel: tc("delete"),
      tone: "danger",
    });
    if (!ok) return;
    setDeleting(item.ref);
    try {
      await api.DELETE("/v1/personas/{persona_id}/artifacts/{ref}", {
        params: { path: { persona_id: personaId, ref: item.ref } },
      });
      notify({ level: "success", title: tn("deleted", { name }) });
      // Reflect immediately (the sheet has no server re-render cycle).
      setRemoved((prev) => new Set(prev).add(item.ref));
      setPreview((p) => (p?.ref === item.ref ? null : p));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div data-slot="artifact-gallery" className="flex flex-col gap-4">
      {/* compact filter row — the v3 chip register */}
      <div className="flex flex-wrap items-center gap-1.5">
        {SOURCE_CHIPS.map((c) => (
          <Chip
            key={c}
            active={sourceFilter === c}
            onClick={() => setSourceFilter(c)}
          >
            {t(`source.${c}`)}
          </Chip>
        ))}
        <span aria-hidden="true" className="mx-1 h-4 w-px bg-border" />
        {TYPE_CHIPS.map((c) => (
          <Chip
            key={c}
            active={typeFilter === c}
            onClick={() => setTypeFilter(c)}
          >
            {t(`type.${c}`)}
          </Chip>
        ))}
        <span className="ml-auto font-mono text-[10px] uppercase tracking-[0.06em] text-muted-foreground">
          {t("countOf", { shown: items.length, total: initial.total })}
        </span>
      </div>

      {items.length === 0 ? (
        <p className="type-body py-12 text-center text-muted-foreground">
          {t("noMatches")}
        </p>
      ) : (
        <ul className="grid grid-cols-2 gap-3" data-slot="artifact-grid">
          {items.map((item) => (
            <li key={item.ref}>
              <ArtifactTile
                personaId={personaId}
                item={item}
                onOpen={() => setPreview(item)}
                onDownload={() => void download(item)}
                onDelete={() => void handleDelete(item)}
                disabled={deleting === item.ref}
              />
            </li>
          ))}
        </ul>
      )}

      {/* preview dialog — images large; documents as an honest meta card */}
      <Dialog.Root
        open={preview !== null}
        onOpenChange={(open) => {
          if (!open) setPreview(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/50 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-sm" />
          <Dialog.Popup className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-50 flex max-h-[85vh] w-[min(44rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-xl outline-none">
            {preview ? (
              <>
                <div className="flex items-center gap-2 border-border border-b px-5 py-3.5">
                  <Dialog.Title className="min-w-0 flex-1 truncate font-heading text-base font-semibold">
                    {displayName(preview)}
                  </Dialog.Title>
                  <span className="shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                    {extBadge(preview)}
                  </span>
                </div>
                <div className="min-h-0 flex-1 overflow-auto">
                  {isImageLike(preview) ? (
                    <div className="grid place-items-center bg-muted/30 p-4">
                      <AuthedImage
                        personaId={personaId}
                        workspacePath={preview.ref}
                        mediaType={preview.media_type}
                        alt={displayName(preview)}
                        className="max-h-[62vh] w-auto rounded-lg object-contain"
                      />
                    </div>
                  ) : (
                    <DocMeta item={preview} />
                  )}
                </div>
                <div className="flex items-center gap-2 border-border border-t px-5 py-3">
                  <Button
                    type="button"
                    className="gap-1.5"
                    onClick={() => void download(preview)}
                  >
                    <Download className="size-4" aria-hidden="true" />
                    {t("download")}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    className="gap-1.5 text-destructive hover:bg-destructive/10"
                    disabled={deleting === preview.ref}
                    onClick={() => void handleDelete(preview)}
                  >
                    <Trash2 className="size-4" aria-hidden="true" />
                    {tc("delete")}
                  </Button>
                  <div className="flex-1" />
                  <Dialog.Close
                    render={
                      <Button type="button" variant="outline">
                        {t("close")}
                      </Button>
                    }
                  />
                </div>
              </>
            ) : null}
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground",
        active && "border-primary/50 bg-primary/10 font-medium text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function DocMeta({ item }: { item: ArtifactItem }) {
  const t = useTranslations("artifacts");
  const Icon = typeIcon(item);
  const rows: [string, string][] = [
    [t("metaSize"), formatBytes(item.size_bytes)],
    [t("metaType"), item.media_type],
  ];
  if (item.metadata?.source)
    rows.push([t("sourceLabel"), t(`source.${item.metadata.source}`)]);
  if (item.metadata?.created_at)
    rows.push([
      t("metaCreated"),
      new Date(item.metadata.created_at).toLocaleString(),
    ]);
  return (
    <div className="flex flex-col items-center gap-4 px-6 py-10">
      <span className="grid size-20 place-items-center rounded-2xl bg-muted">
        <Icon className="size-9 text-muted-foreground" aria-hidden="true" />
      </span>
      <dl className="w-full max-w-xs text-sm">
        {rows.map(([k, v]) => (
          <div
            key={k}
            className="flex justify-between gap-4 border-border/60 border-b py-2 last:border-0"
          >
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="truncate text-right">{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function ArtifactTile({
  personaId,
  item,
  onOpen,
  onDownload,
  onDelete,
  disabled,
}: {
  personaId: string;
  item: ArtifactItem;
  onOpen: () => void;
  onDownload: () => void;
  onDelete: () => void;
  disabled: boolean;
}) {
  const t = useTranslations("artifacts");
  const name = displayName(item);
  const Icon = typeIcon(item);

  return (
    <div
      className="group relative overflow-hidden rounded-xl border border-border bg-card transition-shadow hover:shadow-md"
      data-slot="artifact-tile"
    >
      <button
        type="button"
        onClick={onOpen}
        className="block w-full outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={t("previewLabel", { name })}
      >
        <div className="aspect-square w-full overflow-hidden bg-muted/50">
          {isImageLike(item) ? (
            <AuthedImage
              personaId={personaId}
              workspacePath={item.ref}
              mediaType={item.media_type}
              alt=""
              className="h-full w-full object-cover transition-transform duration-200 group-hover:scale-[1.03]"
            />
          ) : (
            <div className="grid h-full w-full place-items-center">
              <span className="flex flex-col items-center gap-2">
                <Icon
                  className="size-9 text-muted-foreground"
                  aria-hidden="true"
                />
                <span className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                  {extBadge(item)}
                </span>
              </span>
            </div>
          )}
        </div>
        <div className="flex flex-col gap-0.5 px-3 py-2.5 text-left">
          <span className="truncate text-sm font-medium">{name}</span>
          <span className="font-mono text-[10px] uppercase tracking-[0.04em] text-muted-foreground">
            {formatBytes(item.size_bytes)}
            {item.metadata?.source
              ? ` · ${t(`source.${item.metadata.source}`)}`
              : ""}
          </span>
        </div>
      </button>

      {/* hover-revealed actions (always visible to keyboard/touch via focus-within) */}
      <div className="absolute top-2 right-2 flex gap-1 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
        <button
          type="button"
          onClick={onDownload}
          aria-label={t("download")}
          className="grid size-7 place-items-center rounded-md border border-border bg-background/90 text-muted-foreground backdrop-blur hover:text-foreground"
        >
          <Download className="size-3.5" aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={onDelete}
          disabled={disabled}
          aria-label={t("deleteLabel", { ref: name })}
          className="grid size-7 place-items-center rounded-md border border-border bg-background/90 text-destructive backdrop-blur hover:bg-destructive/10 disabled:opacity-50"
        >
          <Trash2 className="size-3.5" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
