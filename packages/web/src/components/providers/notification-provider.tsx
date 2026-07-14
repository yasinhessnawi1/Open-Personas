"use client";

/**
 * Spec 35 cluster L (D-35-10 / D-35-11) — global notification system.
 *
 * `<NotificationProvider>` mounts once in the app shell; `useNotify()` returns
 * `notify({ level, title, body?, persist? })`, the SINGLE façade for every
 * user-facing message:
 *
 *   1. it surfaces an immediate toast through the existing sonner layer
 *      ([patterns/toast.tsx](../patterns/toast.tsx)) — NOT a second toast system;
 *   2. when `persist` is set (default by level — error + consequential success
 *      persist, transient info/warning do not), it prepends an entry to the
 *      bell feed, a client-side `localStorage`-backed list capped at
 *      {@link FEED_CAP} (D-35-11). No backend: the feed is observable-events-only.
 *
 * The bell ([notification-bell.tsx](../shell/notification-bell.tsx)) reads the
 * feed + unread count off this context. Deferred post-v1 (D-35-11): server-side
 * store, background-run alerting, cross-device sync, email/push.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { toast } from "@/components/patterns/toast";

export type NotifyLevel = "success" | "error" | "info" | "warning";

export interface NotifyOptions {
  level: NotifyLevel;
  /** The headline — already localised by the caller (next-intl). */
  title: string;
  /** Optional supporting line — already localised by the caller. */
  body?: string;
  /**
   * Whether this lands in the persistent bell feed. Defaults by level:
   * error + success persist (consequential), info + warning are transient
   * toasts unless the caller opts in (e.g. a low-balance warning passes true).
   */
  persist?: boolean;
  /**
   * Optional in-app deep-link (P6-D-10). When set, the bell entry becomes a
   * clickable row that routes here (e.g. `/runs/{id}`, `/personas/{id}`,
   * `/settings`). Persisted with the entry; only meaningful for persisted
   * notifications. Must be an app-relative path.
   */
  href?: string;
  /**
   * R9-050 — pass the id returned by an earlier {@link notifyLoading} (or
   * `notify`) call to UPDATE that SAME toast in place (sonner's documented
   * id-keyed replace) instead of stacking a second one. Lets a durable
   * background job (e.g. turn-into-file's `file_extract`) swap its
   * persistent "in progress" toast for the eventual success/error outcome.
   * Sonner auto-assigns a NUMERIC id when the caller doesn't pass one (e.g.
   * a bare `notifyLoading` call) — hence the union, not just `string`.
   */
  id?: string | number;
}

export interface NotificationEntry {
  id: string;
  level: NotifyLevel;
  title: string;
  body?: string;
  /** Optional in-app deep-link target (P6-D-10). */
  href?: string;
  /** Epoch ms — when the notification fired. */
  at: number;
  read: boolean;
}

interface NotificationContextValue {
  /** Returns the underlying sonner toast id (see {@link NotifyOptions.id}). */
  notify: (options: NotifyOptions) => string | number;
  entries: readonly NotificationEntry[];
  unreadCount: number;
  markAllRead: () => void;
  /** Mark a single entry read (P6-D-10 — a deep-linked row is read on click). */
  markRead: (id: string) => void;
  clear: () => void;
  /**
   * R9-050 — a persistent "in progress" toast for a durable background job
   * (fire → the job runs out of band → the UI must not go quiet before the
   * result lands). Never enters the bell feed (there is no outcome yet to be
   * consequential about). Returns the toast id; hand it back to `notify` via
   * `{ id }` once the job resolves to swap this SAME toast to success/error
   * rather than leaving it hanging or stacking a second toast.
   */
  notifyLoading: (
    title: string,
    opts?: { id?: string | number },
  ) => string | number;
}

/** Max persisted entries kept in the bell feed (D-35-11: ~20–50). */
export const FEED_CAP = 30;
const STORAGE_KEY = "open-persona:notifications";

const NotificationContext = createContext<NotificationContextValue | null>(
  null,
);

export function useNotify(): NotificationContextValue {
  const ctx = useContext(NotificationContext);
  if (ctx === null) {
    throw new Error("useNotify must be used within a <NotificationProvider>");
  }
  return ctx;
}

/** Persist defaults by level — error + success are consequential. */
function persistsByDefault(level: NotifyLevel): boolean {
  return level === "error" || level === "success";
}

function loadFeed(): NotificationEntry[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    // Defensive: only keep well-shaped entries (a schema bump or hand-edit
    // shouldn't crash hydration).
    return parsed.filter(
      (e): e is NotificationEntry =>
        typeof e === "object" &&
        e !== null &&
        typeof (e as NotificationEntry).id === "string" &&
        typeof (e as NotificationEntry).title === "string",
    );
  } catch {
    return [];
  }
}

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<NotificationEntry[]>([]);

  // Hydrate from localStorage after mount (avoids an SSR/client mismatch — the
  // server has no localStorage, so the first paint is an empty feed).
  useEffect(() => {
    setEntries(loadFeed());
  }, []);

  const persistFeed = useCallback((next: NotificationEntry[]) => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      // Quota / disabled storage — the in-memory feed still works this session.
    }
  }, []);

  const notify = useCallback(
    ({ level, title, body, persist, href, id }: NotifyOptions) => {
      // 1. Immediate toast through the existing sonner layer. `id` (R9-050)
      // makes this an UPDATE of an existing toast (e.g. a loading → success
      // swap) rather than a new one when one is passed.
      const toastOpts: { description?: string; id?: string | number } = {};
      if (body) toastOpts.description = body;
      if (id) toastOpts.id = id;
      const toastId = toast[level](
        title,
        Object.keys(toastOpts).length > 0 ? toastOpts : undefined,
      );

      // 2. Persist into the bell feed when consequential.
      if (persist ?? persistsByDefault(level)) {
        setEntries((cur) => {
          const entry: NotificationEntry = {
            id:
              typeof crypto !== "undefined" && "randomUUID" in crypto
                ? crypto.randomUUID()
                : `${title}-${cur.length}-${level}`,
            level,
            title,
            body,
            href,
            at: Date.now(),
            read: false,
          };
          const next = [entry, ...cur].slice(0, FEED_CAP);
          persistFeed(next);
          return next;
        });
      }
      return toastId;
    },
    [persistFeed],
  );

  // R9-050 — a loading toast has no outcome yet, so it never touches the bell
  // feed; it's the sonner-native `loading` variant, id-addressable so a later
  // `notify({ id, level: "success" | "error" })` swaps it in place.
  const notifyLoading = useCallback(
    (title: string, opts?: { id?: string | number }): string | number =>
      toast.loading(title, opts?.id ? { id: opts.id } : undefined),
    [],
  );

  const markAllRead = useCallback(() => {
    setEntries((cur) => {
      if (cur.every((e) => e.read)) return cur;
      const next = cur.map((e) => (e.read ? e : { ...e, read: true }));
      persistFeed(next);
      return next;
    });
  }, [persistFeed]);

  const markRead = useCallback(
    (id: string) => {
      setEntries((cur) => {
        const target = cur.find((e) => e.id === id);
        if (!target || target.read) return cur;
        const next = cur.map((e) => (e.id === id ? { ...e, read: true } : e));
        persistFeed(next);
        return next;
      });
    },
    [persistFeed],
  );

  const clear = useCallback(() => {
    setEntries([]);
    persistFeed([]);
  }, [persistFeed]);

  const unreadCount = useMemo(
    () => entries.reduce((n, e) => n + (e.read ? 0 : 1), 0),
    [entries],
  );

  const value = useMemo<NotificationContextValue>(
    () => ({
      notify,
      entries,
      unreadCount,
      markAllRead,
      markRead,
      clear,
      notifyLoading,
    }),
    [notify, entries, unreadCount, markAllRead, markRead, clear, notifyLoading],
  );

  return (
    <NotificationContext.Provider value={value}>
      {children}
    </NotificationContext.Provider>
  );
}
