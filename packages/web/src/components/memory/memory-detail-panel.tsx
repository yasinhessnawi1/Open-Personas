"use client";

import {
  BadgeCheck,
  Clock,
  Fingerprint,
  Heart,
  Lightbulb,
  Link2,
  type LucideIcon,
  MapPin,
  MessageSquare,
  Quote,
  Shield,
  Sparkles,
  Target,
  Trash2,
  X,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { Button } from "@/components/ui/button";
import { type MemoryNodeDetail, unwrap } from "@/lib/api";
import { useApi } from "@/lib/api/use-api";
import {
  CARE_COLOR,
  kindColor,
  linkEncoding,
  linkRelationKey,
  linkSortRank,
} from "@/lib/memory/encoding";

/** NodeKind → a glyph for the panel header's colour-square (design-match). */
const KIND_ICON: Record<string, LucideIcon> = {
  concept: Lightbulb,
  fact: BadgeCheck,
  preference: Heart,
  trait: Fingerprint,
  goal: Target,
  circumstance: Quote,
  entity: MapPin,
};

/** Up-to-two-letter initials for the provenance avatar ("Astrid" → "AS"). */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.trim().slice(0, 2).toUpperCase();
}

/** A stable per-persona avatar hue derived from the name (design's coloured avatars). */
function avatarColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++)
    hash = (hash * 31 + name.charCodeAt(i)) % 360;
  return `oklch(0.6 0.14 ${hash})`;
}

/**
 * Spec K5 — the editorial detail panel (K5-D-5 / K5-D-7 / K5-D-10).
 *
 * Reads one node's full detail and renders it as story, not audit: provenance
 * as "where this came from", an evolution timeline of "how it grew", traversable
 * typed links, and — when K4-marked — a care-first sensitive note (never a
 * warning). Control is always present: **content-only** correction (the title
 * affordance is intentionally hidden, K5-D-5a, because `concept_name` may be an
 * entity key) wired to `PATCH`, and a consequence-language deletion wired to
 * `DELETE`. Correction flips provenance to "edited by you" — the graph's most
 * trustworthy write (criterion 6).
 */
export function MemoryDetailPanel({
  nodeId,
  onClose,
  onTraverse,
  onDeleted,
  initialDetail,
}: {
  nodeId: string | null;
  onClose: () => void;
  onTraverse: (id: string) => void;
  onDeleted: (id: string) => void;
  /**
   * Dev/demo seam (the `scratch/memory` harness): pre-seed the detail so the
   * editorial panel renders without a live fetch. Omitted in production — the
   * real page always fetches `GET /nodes/{id}`.
   */
  initialDetail?: MemoryNodeDetail;
}) {
  const t = useTranslations("memory");
  const format = useFormatter();
  const router = useRouter();
  const api = useApi();
  const { notify } = useNotify();
  const confirm = useConfirm();

  const [detail, setDetail] = useState<MemoryNodeDetail | null>(
    initialDetail ?? null,
  );
  const [draft, setDraft] = useState(initialDetail?.content ?? "");
  const [busy, setBusy] = useState(false);

  // `useApi()` / `notify` / `t` are not stable identities — keep them in refs so
  // the fetch effect depends ONLY on `nodeId` (putting them in the deps would
  // re-fire the effect every render → `setDetail(null)` loop while a node is open).
  const apiRef = useRef(api);
  apiRef.current = api;
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const tRef = useRef(t);
  tRef.current = t;

  useEffect(() => {
    if (!nodeId) {
      setDetail(null);
      return;
    }
    // Demo seam: render the pre-seeded detail without a live fetch.
    if (initialDetail && initialDetail.id === nodeId) {
      setDetail(initialDetail);
      setDraft(initialDetail.content);
      return;
    }
    setDetail(null);
    let cancelled = false;
    (async () => {
      try {
        const data = await unwrap(
          await apiRef.current.GET("/v1/memory/nodes/{node_id}", {
            params: { path: { node_id: nodeId } },
          }),
        );
        if (!cancelled) {
          setDetail(data);
          setDraft(data.content);
        }
      } catch {
        if (!cancelled)
          notifyRef.current({
            level: "error",
            title: tRef.current("loadError"),
          });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [nodeId, initialDetail]);

  const dirty =
    detail !== null &&
    draft.trim() !== detail.content.trim() &&
    draft.trim().length > 0;
  const open = nodeId !== null;

  const save = async () => {
    if (!detail || !dirty) return;
    setBusy(true);
    try {
      const updated = await unwrap(
        await api.PATCH("/v1/memory/nodes/{node_id}", {
          params: { path: { node_id: detail.id } },
          body: { content: draft.trim() },
        }),
      );
      setDetail(updated);
      setDraft(updated.content);
      notify({ level: "success", title: t("saved"), body: t("savedBody") });
    } catch {
      notify({ level: "error", title: t("saveError") });
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!detail) return;
    const linkCount = detail.links.length;
    const ok = await confirm({
      title: t("deleteTitle", { label: detail.label }),
      description: t("deleteBody", { count: linkCount }),
      confirmLabel: t("deleteConfirm"),
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await unwrap(
        await api.DELETE("/v1/memory/nodes/{node_id}", {
          params: { path: { node_id: detail.id } },
        }),
      );
      notify({ level: "success", title: t("deleted"), body: t("deletedBody") });
      onDeleted(detail.id);
    } catch {
      notify({ level: "error", title: t("deleteError") });
      setBusy(false);
    }
  };

  // The source conversation this memory was learned in (provenance-as-story lets
  // the user trace it back). Enabled only when the provenance carries a reference.
  const conversationRef = detail?.origin.interaction_id ?? null;
  const openConversation = () => {
    if (conversationRef) router.push(`/chat/${conversationRef}`);
  };

  const sortedLinks = detail
    ? [...detail.links].sort(
        (a, b) => linkSortRank(a.link_type) - linkSortRank(b.link_type),
      )
    : [];
  const lastSource = detail?.evolution.at(-1)?.source ?? detail?.origin.source;
  const userEdited = lastSource === "user";
  const personaName = detail?.origin.persona_name ?? null;

  return (
    <aside
      aria-label={t("detailLabel")}
      data-open={open}
      className="absolute inset-y-0 right-0 z-20 flex w-[396px] max-w-[92vw] flex-col border-l bg-card shadow-[var(--elevation-2)] transition-transform duration-300 ease-out data-[open=false]:translate-x-full motion-reduce:transition-none"
    >
      <button
        type="button"
        aria-label={t("close")}
        onClick={onClose}
        className="absolute right-4 top-4 grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <X className="size-4" />
      </button>

      {detail ? (
        <>
          <div className="flex-1 overflow-y-auto px-5 pt-6">
            {/* header — kind icon-square + kind caption + plain title (title-edit
                hidden, K5-D-5a) */}
            <div className="flex items-start gap-3 pr-8">
              {(() => {
                const KindIcon = KIND_ICON[detail.kind] ?? Quote;
                return (
                  <div
                    className="grid size-10 shrink-0 place-items-center rounded-lg text-white"
                    style={{ background: kindColor(detail.kind) }}
                  >
                    <KindIcon className="size-5" />
                  </div>
                );
              })()}
              <div className="min-w-0 flex-1">
                <p className="type-caption text-muted-foreground">
                  {t(`kind.${detail.kind}`)}
                  {detail.wellbeing_category ? ` · ${t("sensitiveTag")}` : ""}
                </p>
                <h2 className="mt-1 type-heading leading-tight">
                  {detail.label}
                </h2>
              </div>
            </div>

            {dirty ? (
              <div className="mt-4 flex items-center gap-2 rounded-lg border border-primary/30 bg-primary/10 px-3 py-2">
                <span className="flex-1 text-sm">{t("editing")}</span>
                <Button size="sm" onClick={save} disabled={busy}>
                  {t("saveCorrection")}
                </Button>
              </div>
            ) : null}

            {/* what's known — content-only correction (as reachable as delete:
                a visibly-editable field + the save-bar above; K5-D-7 / K5-D-5) */}
            <section className="mt-6">
              <p className="type-caption mb-1.5 text-muted-foreground">
                {t("whatsKnown")}
              </p>
              <textarea
                aria-label={t("whatsKnown")}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={3}
                className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm leading-relaxed hover:border-ring/50 focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/30"
              />
              <p className="type-caption mt-1.5 normal-case tracking-normal text-muted-foreground">
                {t("whatsKnownHint")}
              </p>
            </section>

            {/* K4 sensitive — care, not flag */}
            {detail.wellbeing_category ? (
              <section className="mt-5">
                <div
                  className="flex items-start gap-2.5 rounded-lg border px-3.5 py-3"
                  style={{
                    borderColor: `color-mix(in oklch, ${CARE_COLOR} 28%, transparent)`,
                    background: `color-mix(in oklch, ${CARE_COLOR} 8%, transparent)`,
                  }}
                >
                  <Shield
                    className="mt-0.5 size-4 shrink-0"
                    style={{ color: CARE_COLOR }}
                  />
                  <div>
                    <b className="text-sm">{t("careTitle")}</b>
                    <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                      {t("careBody")}
                    </p>
                  </div>
                </div>
              </section>
            ) : null}

            {/* where this came from */}
            <section className="mt-6">
              <p className="type-caption mb-2 flex items-center gap-1.5 text-muted-foreground">
                <Quote className="size-3" /> {t("provenanceHead")}
              </p>
              <div className="flex gap-3 rounded-lg border border-primary/20 bg-primary/5 px-3.5 py-3">
                <div
                  className="grid size-8 shrink-0 place-items-center rounded-full text-xs font-semibold text-white"
                  style={{
                    background: userEdited
                      ? "var(--primary)"
                      : personaName
                        ? avatarColor(personaName)
                        : "var(--muted-foreground)",
                  }}
                >
                  {userEdited ? (
                    t("you")
                  ) : personaName ? (
                    initials(personaName)
                  ) : (
                    <Sparkles className="size-4" />
                  )}
                </div>
                <div className="min-w-0">
                  <p className="text-sm leading-relaxed">
                    {userEdited
                      ? t("provUser")
                      : (detail.origin.reason ??
                        t(`provSource.${detail.origin.source}`))}
                  </p>
                  <p className="type-caption mt-1.5 normal-case tracking-normal text-muted-foreground">
                    {userEdited
                      ? t("editedByYou")
                      : personaName
                        ? t("learnedBy", { name: personaName })
                        : t(`provSource.${detail.origin.source}`)}
                    {" · "}
                    {format.dateTime(new Date(detail.created_at), {
                      dateStyle: "medium",
                    })}
                  </p>
                </div>
              </div>
            </section>

            {/* how it grew */}
            {detail.evolution.length > 0 ? (
              <section className="mt-6">
                <p className="type-caption mb-2 flex items-center gap-1.5 text-muted-foreground">
                  <Clock className="size-3" /> {t("evolutionHead")}
                </p>
                <ul className="m-0 list-none p-0">
                  {detail.evolution.map((e, i) => (
                    <li
                      key={`${e.written_at}-${i}`}
                      className="relative pb-4 pl-5 last:pb-0 before:absolute before:left-1 before:top-1 before:size-2 before:rounded-full before:bg-muted-foreground after:absolute after:left-[7px] after:top-3 after:bottom-0 after:w-px after:bg-border last:after:hidden data-[user=true]:before:bg-primary"
                      data-user={e.source === "user"}
                    >
                      <div className="text-sm leading-snug">
                        {e.reason ?? t(`evoSource.${e.source}`)}
                      </div>
                      <div className="type-caption mt-0.5 normal-case tracking-normal text-muted-foreground">
                        {format.dateTime(new Date(e.written_at), {
                          dateStyle: "medium",
                        })}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            {/* connected to — traversable typed links */}
            <section className="mb-2 mt-6">
              <p className="type-caption mb-2 flex items-center gap-1.5 text-muted-foreground">
                <Link2 className="size-3" />{" "}
                {t("connectedHead", { count: sortedLinks.length })}
              </p>
              <div className="flex flex-col gap-1.5">
                {sortedLinks.map((link) => {
                  const enc = linkEncoding(link.link_type);
                  const dotted = link.link_type === "semantic";
                  return (
                    <button
                      type="button"
                      key={`${link.neighbor.id}-${link.link_type}`}
                      onClick={() => onTraverse(link.neighbor.id)}
                      className="grid grid-cols-[auto_1fr_auto] items-center gap-2.5 rounded-md border px-3 py-2 text-left hover:border-primary/30 hover:bg-muted"
                    >
                      <span
                        className="w-5 shrink-0"
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
                      <span className="truncate text-sm">
                        {link.neighbor.label}
                      </span>
                      <span className="type-caption normal-case text-muted-foreground">
                        {t(`linkGloss.${linkRelationKey(link.link_type)}`)}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          </div>

          {/* footer — trace back to the source, and the deletion lever */}
          <div className="flex gap-2.5 border-t px-5 py-4">
            <Button
              variant="outline"
              className="flex-1"
              onClick={openConversation}
              disabled={!conversationRef || busy}
            >
              <MessageSquare className="size-4" /> {t("openConversation")}
            </Button>
            <Button
              variant="ghost"
              onClick={remove}
              disabled={busy}
              className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 className="size-4" /> {t("delete")}
            </Button>
          </div>
        </>
      ) : open ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          {t("loadingDetail")}
        </div>
      ) : null}
    </aside>
  );
}
