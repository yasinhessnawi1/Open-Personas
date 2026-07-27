"use client";

/**
 * ⌘K command palette (Spec 35 D-35-14) — the sidebar's command/search affordance.
 *
 * R11-B5 — the persona-web v3 register (ui_kits 3 command-palette.html):
 *   · RECENTS — the last few selections (localStorage), shown when the query is
 *     empty; resolved against live data, never a stale ghost row.
 *   · PERSONA DRILL-IN — Enter/→ on a persona row scopes the palette to that
 *     persona (chip in the input, Backspace/Esc pops): Open chat / Call / Open /
 *     Edit / Files + that persona's conversations. Chat + Call ride the REAL
 *     doors (the `startChat` / `startVoice` server actions — the same ones the
 *     library card and pickers use).
 *   · MATCH HIGHLIGHT — the matched substring reads emphasised.
 *   · A footer key-hint bar (↑↓ · ↵ · → · esc).
 *
 * Opening: the platform-aware shortcut (⌘K on macOS, Ctrl+K on Windows/Linux)
 * via a window keydown listener, AND a decoupled `open-command-palette` custom
 * event so the sidebar's `.v-cmd` trigger (and anything else) can open it
 * without prop-drilling across the server/client shell boundary.
 */

import { Dialog } from "@base-ui/react/dialog";
import {
  Activity,
  Cable,
  CalendarClock,
  ChevronRight,
  FolderOpen,
  ListChecks,
  MessageSquare,
  MessagesSquare,
  Mic,
  Phone,
  Plus,
  Search,
  Settings,
  Sparkles,
  Waypoints,
  X,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { startChat, startVoice } from "@/app/actions";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { cn } from "@/lib/utils";
import type { SidebarData } from "./sidebar-data";

/** The custom event any trigger dispatches to open the palette. */
export const OPEN_COMMAND_PALETTE_EVENT = "open-command-palette";

type ScopePersona = SidebarData["personas"][number];

type CommandGroup =
  | "groupRecent"
  | "groupActions"
  | "groupNavigate"
  | "groupPersonas"
  | "groupConversations"
  | "groupPersona";

const GROUP_ORDER: readonly CommandGroup[] = [
  "groupRecent",
  "groupActions",
  "groupNavigate",
  "groupPersonas",
  "groupConversations",
  "groupPersona",
];

interface CommandItem {
  readonly id: string;
  readonly group: CommandGroup;
  readonly label: string;
  readonly sublabel?: string;
  /** A persona to render its real identity avatar (keep real avatars, D-35-9). */
  readonly persona?: ScopePersona;
  /** A lucide icon for non-persona rows. */
  readonly icon?: typeof Activity;
  /** What selecting does (navigate / server action). Absent on drill-only rows. */
  readonly run?: () => void;
  /** Set on persona rows: Enter/→ scopes the palette to this persona. */
  readonly drill?: ScopePersona;
  /** Serialized identity for the recents ledger ("nav:/x" · "persona:id" · "conv:id"). */
  readonly recentKey?: string;
}

const RECENTS_KEY = "op-palette-recents";
const RECENTS_MAX = 5;

function readRecents(): string[] {
  try {
    const raw = window.localStorage.getItem(RECENTS_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(parsed)
      ? parsed.filter((k) => typeof k === "string")
      : [];
  } catch {
    return [];
  }
}

function pushRecent(key: string) {
  try {
    const next = [key, ...readRecents().filter((k) => k !== key)].slice(
      0,
      RECENTS_MAX,
    );
    window.localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // storage unavailable — recents simply don't persist
  }
}

/** macOS uses ⌘; everything else uses Ctrl. Resolved post-mount (navigator). */
function detectMac(): boolean {
  if (typeof navigator === "undefined") return false;
  return /mac|iphone|ipad|ipod/i.test(
    navigator.platform || navigator.userAgent,
  );
}

/** The matched substring, emphasised (kit match-highlight). */
function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim().toLowerCase();
  if (!q) return <>{text}</>;
  const at = text.toLowerCase().indexOf(q);
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <span className="font-semibold text-primary">
        {text.slice(at, at + q.length)}
      </span>
      {text.slice(at + q.length)}
    </>
  );
}

export function CommandPalette({ data }: { data: SidebarData }) {
  const t = useTranslations("nav.command");
  const tn = useTranslations("nav");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [scope, setScope] = useState<ScopePersona | null>(null);
  const [recentKeys, setRecentKeys] = useState<string[]>([]);
  const [isMac, setIsMac] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listId = useId();

  useEffect(() => setIsMac(detectMac()), []);

  // Open via the platform shortcut + the custom event; toggle on the shortcut.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = isMac ? e.metaKey : e.ctrlKey;
      if (mod && !e.altKey && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen);
    };
  }, [isMac]);

  // Fresh state per open; recents read once per open (localStorage is sync).
  useEffect(() => {
    if (open) {
      setQuery("");
      setScope(null);
      setRecentKeys(readRecents());
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    setScope(null);
  }, []);

  const go = useCallback(
    (href: string) => {
      close();
      router.push(href);
    },
    [router, close],
  );

  // The full candidate set (built once per data/scope change), then filtered.
  const all = useMemo<CommandItem[]>(() => {
    if (scope) {
      // ---- persona-scoped: the drill-in actions + that persona's chats ----
      const p = scope;
      const scoped: CommandItem[] = [
        {
          id: "p-chat",
          group: "groupPersona",
          label: t("actionChat", { name: p.name }),
          icon: MessageSquare,
          run: () => {
            close();
            void startChat(p.id); // the REAL door: create + redirect
          },
        },
        {
          id: "p-call",
          group: "groupPersona",
          label: t("actionCall", { name: p.name }),
          icon: Mic,
          run: () => {
            close();
            void startVoice(p.id);
          },
        },
        {
          id: "p-open",
          group: "groupPersona",
          label: t("actionOpen", { name: p.name }),
          icon: Sparkles,
          run: () => go(`/personas/${p.id}`),
        },
        {
          id: "p-files",
          group: "groupPersona",
          label: t("actionFiles", { name: p.name }),
          icon: FolderOpen,
          run: () => go(`/personas/${p.id}/files`),
        },
      ];
      const convs: CommandItem[] = data.conversations
        .filter((c) => c.persona?.id === p.id)
        .map((c) => ({
          id: `conv-${c.id}`,
          group: "groupConversations" as const,
          label: c.title?.trim() || p.name,
          persona: c.persona ?? undefined,
          recentKey: `conv:${c.id}`,
          run: () => go(`/chat/${c.id}`),
        }));
      return [...scoped, ...convs];
    }

    const actions: CommandItem[] = [
      {
        id: "new-persona",
        group: "groupActions",
        label: t("newPersona"),
        icon: Plus,
        recentKey: "nav:/personas/new",
        run: () => go("/personas/new"),
      },
      {
        id: "new-routine",
        group: "groupActions",
        label: t("newRoutine"),
        icon: CalendarClock,
        recentKey: "nav:/schedule",
        run: () => go("/schedule"),
      },
    ];
    // R11-B1: Home retired (D-R11-1) — Activity is the landing surface. Tasks
    // goes to the consolidated Activity area, not the /runs drill (D-R11-4).
    const NAV: {
      key: string;
      href: string;
      icon: typeof Activity;
      hidden?: boolean;
    }[] = [
      { key: "activity", href: "/activity", icon: Activity },
      { key: "personas", href: "/personas", icon: Sparkles },
      { key: "conversations", href: "/conversations", icon: MessagesSquare },
      { key: "tasks", href: "/activity/tasks", icon: ListChecks },
      { key: "calls", href: "/calls", icon: Phone },
      {
        key: "memory",
        href: "/memory",
        icon: Waypoints,
        hidden: !data.memoryAvailable,
      },
      { key: "schedule", href: "/schedule", icon: CalendarClock },
      { key: "connectors", href: "/settings/connectors", icon: Cable },
      { key: "settings", href: "/settings", icon: Settings },
    ];
    const nav: CommandItem[] = NAV.filter((n) => !n.hidden).map((n) => ({
      id: `nav-${n.key}`,
      group: "groupNavigate" as const,
      label: tn(n.key),
      icon: n.icon,
      recentKey: `nav:${n.href}`,
      run: () => go(n.href),
    }));
    const personas: CommandItem[] = data.personas.map((p) => ({
      id: `persona-${p.id}`,
      group: "groupPersonas" as const,
      label: p.name,
      sublabel: p.role,
      persona: p,
      drill: p,
      recentKey: `persona:${p.id}`,
    }));
    const conversations: CommandItem[] = data.conversations.map((c) => ({
      id: `conv-${c.id}`,
      group: "groupConversations" as const,
      label: c.title?.trim() || (c.persona?.name ?? ""),
      sublabel: c.persona?.name,
      persona: c.persona ?? undefined,
      recentKey: `conv:${c.id}`,
      run: () => go(`/chat/${c.id}`),
    }));
    return [...actions, ...nav, ...personas, ...conversations];
  }, [data, scope, t, tn, go, close]);

  // RECENTS (kit): the last selections, resolved against the LIVE candidate set —
  // a deleted conversation or persona simply stops appearing. Query empty only.
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q) {
      return all.filter(
        (it) =>
          it.label.toLowerCase().includes(q) ||
          it.sublabel?.toLowerCase().includes(q),
      );
    }
    if (scope || recentKeys.length === 0) return all;
    const byKey = new Map(
      all
        .filter((it) => it.recentKey)
        .map((it) => [it.recentKey as string, it]),
    );
    const recents = recentKeys
      .map((k) => byKey.get(k))
      .filter((it): it is CommandItem => it !== undefined)
      .map((it) => ({
        ...it,
        id: `recent-${it.id}`,
        group: "groupRecent" as const,
      }));
    return [...recents, ...all];
  }, [all, query, scope, recentKeys]);

  // Reset highlight when the result set changes; focus the input on open.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-clamp on results length too.
  useEffect(() => setActive(0), [query, open, scope]);

  const drillInto = useCallback((p: ScopePersona) => {
    setScope(p);
    setQuery("");
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const select = useCallback(
    (item: CommandItem | undefined) => {
      if (!item) return;
      if (item.drill) {
        drillInto(item.drill);
        return;
      }
      if (item.recentKey) pushRecent(item.recentKey);
      item.run?.();
    },
    [drillInto],
  );

  const popScope = useCallback(() => {
    setScope(null);
    setQuery("");
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const onInputKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(results.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      select(results[active]);
    } else if (e.key === "ArrowRight" && results[active]?.drill) {
      e.preventDefault();
      drillInto(results[active].drill as ScopePersona);
    } else if (e.key === "Backspace" && query === "" && scope) {
      e.preventDefault();
      popScope();
    } else if (e.key === "Escape" && scope) {
      // Esc pops the persona scope first; a second Esc closes (Dialog default).
      e.preventDefault();
      e.stopPropagation();
      popScope();
    }
  };

  // Group the (filtered) results in a stable order for rendering.
  const grouped = useMemo(() => {
    const flatIndex = new Map<string, number>();
    for (const [i, r] of results.entries()) flatIndex.set(r.id, i);
    return GROUP_ORDER.map((g) => ({
      group: g,
      items: results.filter((r) => r.group === g),
    }))
      .filter((s) => s.items.length > 0)
      .map((s) => ({ ...s, flatIndex }));
  }, [results]);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup
          className={cn(
            "-translate-x-1/2 fixed top-[12vh] left-1/2 z-50 flex max-h-[70vh] w-[min(92vw,600px)] flex-col overflow-hidden rounded-[var(--radius-xl)] border border-border bg-popover text-popover-foreground shadow-[var(--elevation-3)]",
            "transition duration-[var(--motion-duration-normal)] ease-[var(--motion-ease-emphasized)] data-ending-style:opacity-0 data-starting-style:opacity-0 data-ending-style:scale-95 data-starting-style:scale-95",
          )}
        >
          <Dialog.Title className="sr-only">{t("open")}</Dialog.Title>
          {/* search row — with the persona-scope chip when drilled in (kit) */}
          <div className="flex items-center gap-2.5 border-border border-b px-4 py-3">
            <Search className="size-4 shrink-0 text-muted-foreground" />
            {scope ? (
              <span
                className="flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-muted/60 py-1 pr-1.5 pl-1.5 text-sm"
                data-slot="palette-scope-chip"
              >
                <PersonaAvatar persona={scope} size="sm" />
                <span className="max-w-28 truncate font-medium">
                  {scope.name}
                </span>
                <button
                  type="button"
                  aria-label={t("clearScope")}
                  onClick={popScope}
                  className="grid size-5 place-items-center rounded-full text-muted-foreground hover:text-foreground"
                >
                  <X className="size-3.5" />
                </button>
              </span>
            ) : null}
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onInputKeyDown}
              placeholder={
                scope
                  ? t("scopePlaceholder", { name: scope.name })
                  : t("placeholder")
              }
              aria-label={t("placeholder")}
              aria-controls={listId}
              className="min-w-0 flex-1 bg-transparent type-body text-foreground outline-none placeholder:text-muted-foreground"
            />
            <kbd className="type-caption normal-case tracking-normal rounded border border-border px-1.5 py-0.5 text-muted-foreground">
              esc
            </kbd>
          </div>

          {/* results */}
          <div
            id={listId}
            role="listbox"
            className="min-h-0 flex-1 overflow-y-auto p-1.5"
          >
            {results.length === 0 ? (
              <p className="px-3 py-6 text-center type-caption normal-case tracking-normal text-muted-foreground">
                {t("empty")}
              </p>
            ) : (
              grouped.map(({ group, items, flatIndex }) => (
                <div key={group} className="mb-1">
                  <p className="type-caption px-2.5 pt-2 pb-1 text-muted-foreground">
                    {t(group)}
                  </p>
                  {items.map((item) => {
                    const idx = flatIndex.get(item.id) ?? 0;
                    const isActive = idx === active;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        role="option"
                        aria-selected={isActive}
                        onClick={() => select(item)}
                        onMouseMove={() => setActive(idx)}
                        data-drill={item.drill?.id}
                        className={cn(
                          "flex w-full items-center gap-2.5 rounded-[var(--radius-md)] px-2.5 py-2 text-left outline-none",
                          isActive
                            ? "bg-sidebar-accent text-sidebar-accent-foreground"
                            : "text-foreground",
                        )}
                      >
                        {item.persona ? (
                          <PersonaAvatar
                            persona={item.persona}
                            size="sm"
                            className="shrink-0"
                          />
                        ) : item.icon ? (
                          <span className="grid size-6 shrink-0 place-items-center text-muted-foreground">
                            <item.icon className="size-4" />
                          </span>
                        ) : null}
                        <span className="flex min-w-0 flex-1 flex-col">
                          <span className="truncate type-ui font-medium">
                            <Highlight text={item.label} query={query} />
                          </span>
                          {item.sublabel ? (
                            <span className="truncate type-caption normal-case tracking-normal text-muted-foreground">
                              {item.sublabel}
                            </span>
                          ) : null}
                        </span>
                        {item.drill ? (
                          <ChevronRight
                            className="size-4 shrink-0 text-muted-foreground"
                            aria-hidden="true"
                          />
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              ))
            )}
          </div>

          {/* footer key-hint bar (kit) */}
          <div className="flex items-center gap-3 border-border border-t px-4 py-2 text-[11px] text-muted-foreground">
            <span className="flex items-center gap-1">
              <kbd className="rounded border border-border px-1 font-mono">
                ↑
              </kbd>
              <kbd className="rounded border border-border px-1 font-mono">
                ↓
              </kbd>
              {t("footerNavigate")}
            </span>
            <span className="flex items-center gap-1">
              <kbd className="rounded border border-border px-1 font-mono">
                ↵
              </kbd>
              {t("footerOpen")}
            </span>
            <span className="flex items-center gap-1">
              <kbd className="rounded border border-border px-1 font-mono">
                →
              </kbd>
              {t("footerPersona")}
            </span>
            <span className="ml-auto font-heading">Open Persona</span>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * The sidebar's command/search trigger (the `.v-cmd` affordance). Dispatches the
 * open event + shows the platform-aware shortcut hint. Client-only kbd label
 * (resolved post-mount to avoid an SSR platform mismatch).
 */
export function CommandTrigger({ collapsed = false }: { collapsed?: boolean }) {
  const t = useTranslations("nav.command");
  const [isMac, setIsMac] = useState(false);
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setIsMac(detectMac());
    setMounted(true);
  }, []);

  const fire = () =>
    window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));

  return (
    <button
      type="button"
      className="v-cmd m-0"
      onClick={fire}
      aria-label={t("open")}
    >
      <Search aria-hidden />
      {!collapsed && (
        <>
          {/* Compact visible label so it never wraps in the narrow rail; the
              full "Search and commands" stays on the button's aria-label. */}
          <span>{t("search")}</span>
          <kbd suppressHydrationWarning>
            {mounted ? (isMac ? "⌘K" : "Ctrl K") : ""}
          </kbd>
        </>
      )}
    </button>
  );
}
