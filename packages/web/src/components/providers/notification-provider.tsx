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
  notify: (options: NotifyOptions) => void;
  entries: readonly NotificationEntry[];
  unreadCount: number;
  markAllRead: () => void;
  /** Mark a single entry read (P6-D-10 — a deep-linked row is read on click). */
  markRead: (id: string) => void;
  clear: () => void;
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
    ({ level, title, body, persist, href }: NotifyOptions) => {
      // 1. Immediate toast through the existing sonner layer.
      toast[level](title, body ? { description: body } : undefined);

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
    },
    [persistFeed],
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
    () => ({ notify, entries, unreadCount, markAllRead, markRead, clear }),
    [notify, entries, unreadCount, markAllRead, markRead, clear],
  );

  return (
    <NotificationContext.Provider value={value}>
      {children}
    </NotificationContext.Provider>
  );
}
