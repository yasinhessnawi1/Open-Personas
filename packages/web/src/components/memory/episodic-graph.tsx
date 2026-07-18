"use client";

import { Trash2, Waypoints, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EmptyState } from "@/components/patterns/empty-state";
import { ExecutorPicker } from "@/components/persona/executor-picker";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { Button } from "@/components/ui/button";
import {
  type EpisodicGistView,
  type EpisodicMemberView,
  type MemoryLinkEdge,
  type MemoryNodeSummary,
  unwrap,
} from "@/lib/api";
import { useApi } from "@/lib/api/use-api";

// The canvas owns a Web Worker (FA2 layout) + a <canvas> — client-only, never SSR'd
// (mirrors memory-view.tsx's own dynamic import).
const MemoryCanvas = dynamic(
  () => import("./memory-canvas").then((m) => m.MemoryCanvas),
  { ssr: false },
);

// Episodic renders in the SAME memory-canvas force layout (D-K11-5), but it is
// its OWN graph — gists and their raw members, not K0 concept nodes. There's no
// dedicated encoding for this shape; borrowing two of the K5 palette's existing
// kinds keeps clusters (concept blue) visually distinct from drilled leaves
// (entity gold) without adding a third colour system just for this view — no
// legend is shown here, so the borrowed kind names are never surfaced as text.
const GIST_KIND = "concept";
const MEMBER_KIND = "entity";
const MEMBERSHIP_LINK = "entity";

/** Collapse a memory's free text into a short, single-line canvas label. */
function truncateLabel(text: string, max = 42): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > max ? `${oneLine.slice(0, max - 1)}…` : oneLine;
}

interface SelectedGist {
  readonly kind: "gist";
  readonly id: string;
  readonly text: string;
  readonly createdAt: string;
  readonly memberCount: number;
  readonly expanded: boolean;
}

interface SelectedMember {
  readonly kind: "member";
  readonly id: string;
  readonly text: string;
  readonly createdAt: string;
}

type Selected = SelectedGist | SelectedMember;

/**
 * Spec K11 (T4, D-K11-5) — the episodic browser: gist cluster-nodes by default
 * (compressed, for scale), expand-on-demand to raw member leaves, membership as
 * edges. Per-persona (episodic is per-persona; D-K11-3) — a picker up top swaps
 * the whole window. Selecting a node opens a lean detail panel; forgetting calls
 * `DELETE /v1/memory/episodic/{id}` (D-K11-6's cascade — a raw chunk takes its
 * covering gist with it, a gist takes its members — so every forget re-fetches
 * the persona's window fresh rather than guessing the cascade client-side).
 */
export function EpisodicGraph({
  personas,
}: {
  personas: readonly AvatarPersona[];
}) {
  const t = useTranslations("memory");
  const api = useApi();
  const { notify } = useNotify();
  const confirm = useConfirm();

  const [personaId, setPersonaId] = useState(personas[0]?.id ?? "");
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(false);
  const [gists, setGists] = useState<readonly EpisodicGistView[]>([]);
  const [members, setMembers] = useState<
    Readonly<Record<string, readonly EpisodicMemberView[]>>
  >({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const onChange = () => setReducedMotion(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // `useApi()` / `notify` / `t` are not stable identities — keep them in refs so
  // the load effect depends only on `personaId` (mirrors memory-detail-panel.tsx).
  const apiRef = useRef(api);
  apiRef.current = api;
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const tRef = useRef(t);
  tRef.current = t;

  const load = useCallback(async (pid: string) => {
    if (!pid) {
      setGists([]);
      setAvailable(true);
      return;
    }
    setLoading(true);
    try {
      const data = await unwrap(
        await apiRef.current.GET("/v1/memory/episodic", {
          params: { query: { persona_id: pid } },
        }),
      );
      setAvailable(data.available);
      setGists(data.gists);
    } catch {
      setAvailable(false);
      setGists([]);
      notifyRef.current({ level: "error", title: tRef.current("loadError") });
    } finally {
      setMembers({});
      setSelectedId(null);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(personaId);
  }, [personaId, load]);

  // Adapt gists → nodes, `member_ids` → edges (once expanded) — the exact shape
  // `<MemoryCanvas>` / `useForceLayout` already draw for the K5 concept graph.
  const { nodes, links } = useMemo(() => {
    const ns: MemoryNodeSummary[] = [];
    const ls: MemoryLinkEdge[] = [];
    for (const g of gists) {
      ns.push({
        id: g.id,
        kind: GIST_KIND,
        label: truncateLabel(g.text),
        wellbeing_category: null,
        degree: g.member_ids.length,
      });
      const loaded = members[g.id];
      if (loaded) {
        for (const m of loaded) {
          ns.push({
            id: m.id,
            kind: MEMBER_KIND,
            label: truncateLabel(m.text),
            wellbeing_category: null,
            degree: 1,
          });
          ls.push({
            src_node_id: g.id,
            dst_node_id: m.id,
            link_type: MEMBERSHIP_LINK,
            weight: null,
          });
        }
      }
    }
    return { nodes: ns, links: ls };
  }, [gists, members]);

  const expand = useCallback(
    async (gistId: string) => {
      if (members[gistId]) return;
      setBusy(true);
      try {
        const data = await unwrap(
          await api.GET("/v1/memory/episodic/{gist_id}/members", {
            params: {
              path: { gist_id: gistId },
              query: { persona_id: personaId },
            },
          }),
        );
        setMembers((prev) => ({ ...prev, [gistId]: data.members }));
      } catch {
        notify({ level: "error", title: t("episodicExpandError") });
      } finally {
        setBusy(false);
      }
    },
    [api, members, personaId, notify, t],
  );

  const collapse = useCallback((gistId: string) => {
    setMembers((prev) => {
      if (!(gistId in prev)) return prev;
      const next = { ...prev };
      delete next[gistId];
      return next;
    });
  }, []);

  const forget = useCallback(
    async (nodeId: string, isGist: boolean) => {
      const ok = await confirm({
        title: t(
          isGist ? "episodicForgetGistTitle" : "episodicForgetMemberTitle",
        ),
        description: t(
          isGist ? "episodicForgetGistBody" : "episodicForgetMemberBody",
        ),
        confirmLabel: t("episodicForgetConfirm"),
        tone: "danger",
      });
      if (!ok) return;
      setBusy(true);
      try {
        await api.DELETE("/v1/memory/episodic/{chunk_id}", {
          params: {
            path: { chunk_id: nodeId },
            query: { persona_id: personaId, is_gist: isGist },
          },
        });
        notify({ level: "success", title: t("episodicForgotten") });
        // The cascade (D-K11-6) removes more than just this node — a raw
        // chunk's forget also removes its covering gist — so the fresh window
        // is re-fetched rather than reconciled locally.
        await load(personaId);
      } catch {
        notify({ level: "error", title: t("episodicForgetError") });
      } finally {
        setBusy(false);
      }
    },
    [api, personaId, confirm, notify, t, load],
  );

  const selected: Selected | null = useMemo(() => {
    if (!selectedId) return null;
    const gist = gists.find((g) => g.id === selectedId);
    if (gist) {
      return {
        kind: "gist",
        id: gist.id,
        text: gist.text,
        createdAt: gist.created_at,
        memberCount: gist.member_ids.length,
        expanded: Boolean(members[gist.id]),
      };
    }
    for (const ms of Object.values(members)) {
      const member = ms.find((m) => m.id === selectedId);
      if (member) {
        return {
          kind: "member",
          id: member.id,
          text: member.text,
          createdAt: member.created_at,
        };
      }
    }
    return null;
  }, [selectedId, gists, members]);

  const hasContent = gists.length > 0;

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center gap-4 px-1 pb-3">
        <ExecutorPicker
          personas={personas}
          value={personaId}
          onSelect={setPersonaId}
          label={t("episodicPersonaLabel")}
          placeholder={t("episodicPersonaPlaceholder")}
        />
        {available && hasContent ? (
          <span className="ml-auto inline-flex items-center gap-1.5 rounded-full border bg-card px-3 py-1.5 type-caption normal-case tracking-normal text-muted-foreground tabular-nums">
            <b className="font-medium text-foreground">{gists.length}</b>{" "}
            {t("episodicGistsWord")}
          </span>
        ) : null}
      </div>

      <div className="relative min-h-[32rem] flex-1 overflow-hidden rounded-xl border bg-background">
        {!personaId ? (
          <EmptyState
            className="h-full justify-center rounded-none border-none bg-transparent"
            icon={<Waypoints className="size-8" aria-hidden="true" />}
            title={t("episodicNoPersona")}
            description={t("episodicNoPersonaHint")}
          />
        ) : !available ? (
          <EmptyState
            className="h-full justify-center rounded-none border-none bg-transparent"
            icon={<Waypoints className="size-8" aria-hidden="true" />}
            title={t("episodicUnavailable")}
            description={t("episodicUnavailableHint")}
          />
        ) : loading && !hasContent ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            {t("loading")}
          </div>
        ) : !hasContent ? (
          <EmptyState
            className="h-full justify-center rounded-none border-none bg-transparent"
            icon={<Waypoints className="size-8" aria-hidden="true" />}
            title={t("episodicEmpty")}
            description={t("episodicEmptyHint")}
          />
        ) : (
          <>
            <MemoryCanvas
              nodes={nodes}
              links={links}
              selectedId={selectedId}
              onSelectNode={setSelectedId}
              reducedMotion={reducedMotion}
            />
            <EpisodicDetailPanel
              node={selected}
              onClose={() => setSelectedId(null)}
              onExpand={() => selected && expand(selected.id)}
              onCollapse={() => selected && collapse(selected.id)}
              onForget={() =>
                selected && forget(selected.id, selected.kind === "gist")
              }
              busy={busy}
            />
          </>
        )}
      </div>
    </div>
  );
}

/**
 * The episodic browser's lean detail panel — provenance-as-story is a K5
 * concept-node concern (correction, evolution, links); episodic nodes are raw
 * text + a forget lever, so this does NOT reuse `<MemoryDetailPanel>` (D-K11-5:
 * "two independent graphs — different memory kinds").
 */
function EpisodicDetailPanel({
  node,
  onClose,
  onExpand,
  onCollapse,
  onForget,
  busy,
}: {
  node: Selected | null;
  onClose: () => void;
  onExpand: () => void;
  onCollapse: () => void;
  onForget: () => void;
  busy: boolean;
}) {
  const t = useTranslations("memory");
  const format = useFormatter();
  const open = node !== null;

  return (
    <aside
      aria-label={t("episodicDetailLabel")}
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

      {node ? (
        <>
          <div className="flex-1 overflow-y-auto px-5 pt-6">
            <p className="type-caption text-muted-foreground">
              {node.kind === "gist"
                ? t("episodicKindGist")
                : t("episodicKindMember")}
            </p>
            <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed">
              {node.text}
            </p>
            <p className="type-caption mt-4 normal-case tracking-normal text-muted-foreground">
              {format.dateTime(new Date(node.createdAt), {
                dateStyle: "medium",
              })}
            </p>
            {node.kind === "gist" ? (
              <p className="type-caption mt-1 normal-case tracking-normal text-muted-foreground">
                {t("episodicMemberCount", { count: node.memberCount })}
              </p>
            ) : null}
          </div>

          <div className="flex gap-2.5 border-t px-5 py-4">
            {node.kind === "gist" ? (
              <Button
                variant="outline"
                className="flex-1"
                onClick={node.expanded ? onCollapse : onExpand}
                disabled={busy}
              >
                {node.expanded ? t("episodicCollapse") : t("episodicExpand")}
              </Button>
            ) : null}
            <Button
              variant="ghost"
              onClick={onForget}
              disabled={busy}
              className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 className="size-4" /> {t("episodicForget")}
            </Button>
          </div>
        </>
      ) : null}
    </aside>
  );
}
