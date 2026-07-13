"use client";

/**
 * ⌘K command palette (Spec 35 D-35-14) — the sidebar's command/search affordance.
 *
 * Lean v1 scope: search personas + conversations + jump to nav routes + quick
 * actions (New persona). Built on the base-ui Dialog primitive (same one that
 * backs the sheet) + existing token utilities — no new dependency.
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
  ListChecks,
  MessagesSquare,
  Plus,
  Search,
  Sparkles,
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
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { cn } from "@/lib/utils";
import type { SidebarData } from "./sidebar-data";

/** The custom event any trigger dispatches to open the palette. */
export const OPEN_COMMAND_PALETTE_EVENT = "open-command-palette";

type CommandGroup =
  | "groupActions"
  | "groupNavigate"
  | "groupPersonas"
  | "groupConversations";

interface CommandItem {
  readonly id: string;
  readonly group: CommandGroup;
  readonly label: string;
  readonly sublabel?: string;
  readonly href: string;
  /** A persona to render its real identity avatar (keep real avatars, D-35-9). */
  readonly persona?: SidebarData["personas"][number];
  /** A lucide icon for non-persona rows. */
  readonly icon?: typeof Activity;
}

/** macOS uses ⌘; everything else uses Ctrl. Resolved post-mount (navigator). */
function detectMac(): boolean {
  if (typeof navigator === "undefined") return false;
  return /mac|iphone|ipad|ipod/i.test(
    navigator.platform || navigator.userAgent,
  );
}

export function CommandPalette({ data }: { data: SidebarData }) {
  const t = useTranslations("nav.command");
  const tn = useTranslations("nav");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
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

  // The full candidate set (built once per data change), then filtered by query.
  const all = useMemo<CommandItem[]>(() => {
    const actions: CommandItem[] = [
      {
        id: "new-persona",
        group: "groupActions",
        label: t("newPersona"),
        href: "/personas/new",
        icon: Plus,
      },
    ];
    const nav: CommandItem[] = [
      // R11-B1: Home retired (D-R11-1) — Activity is the landing surface, and
      // Connectors joined the top-level nav (D-R11-3). Tasks goes to the
      // consolidated Activity area, not the /runs execution drill (D-R11-4).
      {
        id: "nav-activity",
        group: "groupNavigate",
        label: tn("activity"),
        href: "/activity",
        icon: Activity,
      },
      {
        id: "nav-personas",
        group: "groupNavigate",
        label: tn("personas"),
        href: "/personas",
        icon: Sparkles,
      },
      {
        id: "nav-conversations",
        group: "groupNavigate",
        label: tn("conversations"),
        href: "/conversations",
        icon: MessagesSquare,
      },
      {
        id: "nav-tasks",
        group: "groupNavigate",
        label: tn("tasks"),
        href: "/activity/tasks",
        icon: ListChecks,
      },
      {
        id: "nav-connectors",
        group: "groupNavigate",
        label: tn("connectors"),
        href: "/connectors",
        icon: Cable,
      },
    ];
    const personas: CommandItem[] = data.personas.map((p) => ({
      id: `persona-${p.id}`,
      group: "groupPersonas",
      label: p.name,
      sublabel: p.role,
      href: `/personas/${p.id}`,
      persona: p,
    }));
    const conversations: CommandItem[] = data.conversations.map((c) => ({
      id: `conv-${c.id}`,
      group: "groupConversations",
      label: c.title?.trim() || (c.persona?.name ?? ""),
      sublabel: c.persona?.name,
      href: `/chat/${c.id}`,
      persona: c.persona ?? undefined,
    }));
    return [...actions, ...nav, ...personas, ...conversations];
  }, [data, t, tn]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (it) =>
        it.label.toLowerCase().includes(q) ||
        it.sublabel?.toLowerCase().includes(q),
    );
  }, [all, query]);

  // Reset highlight when the result set changes; focus the input on open.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-clamp on results length too.
  useEffect(() => setActive(0), [query, open]);
  useEffect(() => {
    if (open) requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  const select = useCallback(
    (item: CommandItem | undefined) => {
      if (!item) return;
      setOpen(false);
      setQuery("");
      router.push(item.href);
    },
    [router],
  );

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
    }
  };

  // Group the (filtered) results in a stable order for rendering.
  const grouped = useMemo(() => {
    const order: CommandGroup[] = [
      "groupActions",
      "groupNavigate",
      "groupPersonas",
      "groupConversations",
    ];
    const flatIndex = new Map<string, number>();
    for (const [i, r] of results.entries()) flatIndex.set(r.id, i);
    return order
      .map((g) => ({ group: g, items: results.filter((r) => r.group === g) }))
      .filter((s) => s.items.length > 0)
      .map((s) => ({ ...s, flatIndex }));
  }, [results]);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup
          className={cn(
            "-translate-x-1/2 fixed top-[12vh] left-1/2 z-50 flex max-h-[70vh] w-[min(92vw,560px)] flex-col overflow-hidden rounded-[var(--radius-xl)] border border-border bg-popover text-popover-foreground shadow-[var(--elevation-3)]",
            "transition duration-[var(--motion-duration-normal)] ease-[var(--motion-ease-emphasized)] data-ending-style:opacity-0 data-starting-style:opacity-0 data-ending-style:scale-95 data-starting-style:scale-95",
          )}
        >
          <Dialog.Title className="sr-only">{t("open")}</Dialog.Title>
          {/* search row */}
          <div className="flex items-center gap-2.5 border-border border-b px-4 py-3">
            <Search className="size-4 shrink-0 text-muted-foreground" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onInputKeyDown}
              placeholder={t("placeholder")}
              aria-label={t("placeholder")}
              aria-controls={listId}
              className="min-w-0 flex-1 bg-transparent type-body text-foreground outline-none placeholder:text-muted-foreground"
            />
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
                  <p className="px-2.5 pt-2 pb-1 type-caption text-muted-foreground">
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
                            {item.label}
                          </span>
                          {item.sublabel ? (
                            <span className="truncate type-caption normal-case tracking-normal text-muted-foreground">
                              {item.sublabel}
                            </span>
                          ) : null}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ))
            )}
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
