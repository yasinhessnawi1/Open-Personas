"use client";

/**
 * Spec A11 — the persistent user-level live channel consumer (`GET /v1/me/events`).
 *
 * Mounted ONCE in the app shell, it holds a single SSE connection (via `consumeSSE`
 * over `fetch` — NOT `EventSource`, which cannot send the Clerk Bearer header,
 * D-09-1) and dispatches the closed, versioned event set to subscribers registered
 * through {@link useMeEvent}. It is the transport ONLY — the consumers (the bell, the
 * open chat) decide what to do.
 *
 * Guarantees (the A11 client contract):
 * - **Refetch-on-ping, never trust pushed state (surface-lags-truth):** an event is a
 *   trigger, not data. `notification.created` → the bell refetches `/v1/me/notifications`;
 *   `message.delivered` → the open chat refetches the conversation. The payload only
 *   decides WHAT to refetch.
 * - **Dedupe by id (A11-D-4):** each data frame carries an `id: <epoch>:<seq>`; a
 *   replayed-or-duplicate event (seq ≤ the last seen, same epoch) is dropped, so a
 *   frame seen on both a reconnect-replay and live never fires twice.
 * - **Resume table (A11-D-3):** `ready`/`resync` carry `{epoch, latest_seq}`. On
 *   reconnect the client sends `Last-Event-ID`; a `resync` (epoch changed / ring gap)
 *   means the server could not honour it → the client does a FULL refetch (fires a
 *   `resync` event to every subscriber) and re-baselines.
 * - **Fail-soft:** a 503 (channel unwired) or a dropped connection never breaks the
 *   shell; it retries with capped backoff while P6's independent poll remains the
 *   durable floor (reload-to-see degrade).
 *
 * A no-op DEFAULT lets `useMeEvent` be called outside the provider (unit tests) — the
 * ServerNotificationsProvider precedent.
 */

import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from "react";
import { useAuth } from "@/auth";
import { ApiError } from "@/lib/api/client";
import { createChannelRouter, type MeEventType } from "@/lib/me-events-router";
import { consumeSSE } from "@/lib/sse";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

export type { MeEventType };

/** A subscriber handler; receives the parsed `data` payload (or `{}` for `resync`). */
export type MeEventHandler = (data: Record<string, unknown>) => void;

interface MeEventsValue {
  subscribe: (type: MeEventType, handler: MeEventHandler) => () => void;
}

const DEFAULT: MeEventsValue = { subscribe: () => () => {} };

const MeEventsContext = createContext<MeEventsValue>(DEFAULT);

/** Subscribe a handler to a live channel event for the component's lifetime. */
export function useMeEvent(type: MeEventType, handler: MeEventHandler): void {
  const { subscribe } = useContext(MeEventsContext);
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(
    () => subscribe(type, (data) => handlerRef.current(data)),
    [type, subscribe],
  );
}

/** Reconnect backoff (ms): fast first retry, capped so an unwired channel is calm. */
const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 20_000] as const;

export function MeEventsProvider({ children }: { children: ReactNode }) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;

  // type → set of handlers, read through a ref so the connection effect (mounted
  // once) always dispatches to the CURRENT subscribers.
  const subsRef = useRef<Map<MeEventType, Set<MeEventHandler>>>(new Map());

  const subscribe = useMemo<MeEventsValue["subscribe"]>(
    () => (type, handler) => {
      const map = subsRef.current;
      const set = map.get(type) ?? new Set<MeEventHandler>();
      set.add(handler);
      map.set(type, set);
      return () => set.delete(handler);
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;

    const router = createChannelRouter((type, data) => {
      const set = subsRef.current.get(type);
      if (!set) return;
      for (const h of set) {
        try {
          h(data);
        } catch {
          // A subscriber's failure never breaks the channel or the other subscribers.
        }
      }
    });

    const run = async () => {
      let attempt = 0;
      while (!cancelled) {
        let jwt: string | null | undefined;
        try {
          jwt = await getTokenRef.current(
            TEMPLATE ? { template: TEMPLATE } : undefined,
          );
        } catch {
          jwt = null;
        }
        controller = new AbortController();
        const lastId = router.lastEventId();
        try {
          for await (const frame of consumeSSE(`${API}/v1/me/events`, {
            headers: {
              ...(jwt ? { Authorization: `Bearer ${jwt}` } : {}),
              ...(lastId ? { "Last-Event-ID": lastId } : {}),
            },
            signal: controller.signal,
          })) {
            if (cancelled) break;
            router.handle(frame);
            attempt = 0; // a delivered frame proves the channel is healthy
          }
        } catch (err) {
          if (cancelled) break;
          // 503 (unwired) or any drop → fail-soft: P6's poll is the floor. Back off
          // harder on a 503 so an unwired deploy isn't hammered.
          if (err instanceof ApiError && err.status === 503) {
            attempt = Math.max(attempt, 2);
          }
        }
        if (cancelled) break;
        const wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
        attempt += 1;
        await new Promise((r) => setTimeout(r, wait));
      }
    };

    void run();

    // Visibility-aware: a tab returning to focus reconnects immediately (aborting the
    // idle-backoff wait) so a delivery that landed while backgrounded surfaces fast.
    const onFocus = () => controller?.abort();
    window.addEventListener("focus", onFocus);

    return () => {
      cancelled = true;
      window.removeEventListener("focus", onFocus);
      controller?.abort();
    };
  }, []);

  const value = useMemo<MeEventsValue>(() => ({ subscribe }), [subscribe]);
  return (
    <MeEventsContext.Provider value={value}>
      {children}
    </MeEventsContext.Provider>
  );
}
