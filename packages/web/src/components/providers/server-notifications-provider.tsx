"use client";

/**
 * Spec P6 (Deliverable 4, D4-e) — the durable, cross-device notification feed.
 *
 * Mounted ONCE in the app shell, it polls `GET /v1/me/notifications` (on load, on
 * window focus, and a light interval) and exposes the server-authored feed
 * (run-terminal + persona-ready, written by D4-c/d) to the bell. This is the
 * cross-device half of the feed (D-P6-7): the bell renders the UNION of this
 * server feed and the client-session advisory (low-balance) from `useNotify()`.
 *
 * Copy is resolved here (P6-D-5): rows carry a locale-neutral `message_key` +
 * `params`; next-intl localises at render. `kind` + `ref_id` derive the deep-link.
 *
 * Toast-on-new with route-match suppression (D-P6-9): the server ALWAYS persists
 * the row (so the bell is correct off-view / cross-device); the client shows a
 * transient toast only for a notification newly seen THIS session — and never for
 * a run-terminal whose run is the route currently in view (you're already looking
 * at it; the bell entry still exists for later). The first poll is a silent
 * baseline so a page load doesn't replay every past notification as a toast.
 *
 * A no-op DEFAULT lets the bell call `useServerNotifications()` outside the
 * provider (unit tests) without a crash — the ActiveWorkProvider precedent.
 */

import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useAuth } from "@/auth";
import { toast } from "@/components/patterns/toast";
import { useMeEvent } from "@/components/providers/me-events-provider";
import type { NotifyLevel } from "@/components/providers/notification-provider";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;
/** Light background refresh so a run finished elsewhere surfaces without a reload. */
const POLL_INTERVAL_MS = 60_000;

/** A server-feed row as returned by `GET /v1/me/notifications`. */
interface ServerNotificationRow {
  id: string;
  kind: string;
  ref_id: string | null;
  level: NotifyLevel;
  message_key: string;
  params: Record<string, string>;
  read: boolean;
  created_at: string;
}

/** A server notification mapped for the bell (title resolved, href derived). */
export interface ServerNotificationEntry {
  id: string;
  level: NotifyLevel;
  title: string;
  href?: string;
  /** Epoch ms (from `created_at`), so the bell can merge-sort with client entries. */
  at: number;
  read: boolean;
  kind: string;
  refId: string | null;
}

interface ServerNotificationsValue {
  readonly entries: readonly ServerNotificationEntry[];
  readonly unreadCount: number;
  markAllRead: () => void;
  markRead: (id: string) => void;
}

const DEFAULT: ServerNotificationsValue = {
  entries: [],
  unreadCount: 0,
  markAllRead: () => {},
  markRead: () => {},
};

const ServerNotificationsContext =
  createContext<ServerNotificationsValue>(DEFAULT);

export function useServerNotifications(): ServerNotificationsValue {
  return useContext(ServerNotificationsContext);
}

/** Deep-link for a server notification (`run_terminal` → run, `persona_ready` → persona). */
function hrefFor(kind: string, refId: string | null): string | undefined {
  if (!refId) return undefined;
  if (kind === "run_terminal") return `/runs/${refId}`;
  if (kind === "persona_ready") return `/personas/${refId}`;
  return undefined;
}

export function ServerNotificationsProvider({
  children,
}: {
  children: ReactNode;
}) {
  const { getToken } = useAuth();
  const t = useTranslations();
  const pathname = usePathname();

  const [rows, setRows] = useState<ServerNotificationRow[]>([]);

  // Read churny identities through refs so the poll effect runs once + stable.
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const pathnameRef = useRef(pathname);
  pathnameRef.current = pathname;
  const tRef = useRef(t);
  tRef.current = t;
  // IDs already surfaced as a toast (or baselined on first load) — never re-toast.
  const seenRef = useRef<Set<string>>(new Set());
  const baselinedRef = useRef(false);

  const authFetch = useCallback(
    async (path: string, init?: RequestInit): Promise<Response | null> => {
      let jwt: string | null | undefined;
      try {
        jwt = await getTokenRef.current(
          TEMPLATE ? { template: TEMPLATE } : undefined,
        );
      } catch {
        return null;
      }
      try {
        return await fetch(`${API}${path}`, {
          ...init,
          headers: {
            ...(init?.headers ?? {}),
            ...(jwt ? { Authorization: `Bearer ${jwt}` } : {}),
          },
        });
      } catch {
        return null; // best-effort — a failed poll never disrupts the app.
      }
    },
    [],
  );

  const resolveTitle = useCallback((row: ServerNotificationRow): string => {
    const persona =
      row.params.persona ?? tRef.current("notifications.personaFallback");
    return tRef.current(row.message_key, { ...row.params, persona });
  }, []);

  const refresh = useCallback(async () => {
    const res = await authFetch("/v1/me/notifications");
    if (!res || !res.ok) return;
    let next: ServerNotificationRow[];
    try {
      next = (await res.json()) as ServerNotificationRow[];
    } catch {
      return;
    }
    if (!Array.isArray(next)) return;

    // Toast newly-seen unread notifications (baseline the first poll silently).
    if (!baselinedRef.current) {
      for (const row of next) seenRef.current.add(row.id);
      baselinedRef.current = true;
    } else {
      for (const row of next) {
        if (seenRef.current.has(row.id)) continue;
        seenRef.current.add(row.id);
        if (row.read) continue;
        // No-double-signal (D-P6-9): suppress the toast for a run-terminal whose
        // run is the current route — the bell entry still lands regardless.
        const onThisRun =
          row.kind === "run_terminal" &&
          row.ref_id !== null &&
          pathnameRef.current === `/runs/${row.ref_id}`;
        if (!onThisRun) {
          toast[row.level](resolveTitle(row));
        }
      }
    }
    setRows(next);
  }, [authFetch, resolveTitle]);

  // Poll: on mount, on window focus, and a light interval. This is the DURABLE FLOOR
  // (P6) + the A11 fail-soft: if the live channel is down, the bell still catches up
  // on the interval / focus (reload-to-see degrade), just not instantly.
  useEffect(() => {
    void refresh();
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      window.removeEventListener("focus", onFocus);
      clearInterval(timer);
    };
  }, [refresh]);

  // Spec A11: make the bell LIVE. A `notification.created` ping (data-only) triggers a
  // refetch of the authoritative feed — never trusting the pushed payload (the client
  // reconciles by id, toasts newly-seen unread). A `resync` (the server couldn't honour
  // our resume cursor) triggers the same full refetch. The union with P6 + read-state
  // is unchanged; A11 only removes the poll-latency.
  const onLiveNotification = useCallback(() => void refresh(), [refresh]);
  useMeEvent("notification.created", onLiveNotification);
  useMeEvent("resync", onLiveNotification);

  const markAllRead = useCallback(() => {
    setRows((cur) => cur.map((r) => (r.read ? r : { ...r, read: true })));
    void authFetch("/v1/me/notifications/read-all", { method: "POST" });
  }, [authFetch]);

  const markRead = useCallback(
    (id: string) => {
      setRows((cur) =>
        cur.map((r) => (r.id === id && !r.read ? { ...r, read: true } : r)),
      );
      void authFetch(`/v1/me/notifications/${encodeURIComponent(id)}/read`, {
        method: "POST",
      });
    },
    [authFetch],
  );

  const entries = useMemo<ServerNotificationEntry[]>(
    () =>
      rows.map((r) => ({
        id: r.id,
        level: r.level,
        title: resolveTitle(r),
        href: hrefFor(r.kind, r.ref_id),
        at: Date.parse(r.created_at) || 0,
        read: r.read,
        kind: r.kind,
        refId: r.ref_id,
      })),
    [rows, resolveTitle],
  );

  const unreadCount = useMemo(
    () => entries.reduce((n, e) => n + (e.read ? 0 : 1), 0),
    [entries],
  );

  const value = useMemo<ServerNotificationsValue>(
    () => ({ entries, unreadCount, markAllRead, markRead }),
    [entries, unreadCount, markAllRead, markRead],
  );

  return (
    <ServerNotificationsContext.Provider value={value}>
      {children}
    </ServerNotificationsContext.Provider>
  );
}
