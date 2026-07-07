"use client";

import { useEffect, useRef } from "react";

/**
 * Spec A6 (W8) — the task.updated consumption SEAM (transport-agnostic).
 *
 * A surface subscribes via {@link useTaskSignal} and REFETCHES its durable data on each signal —
 * never trusting a pushed state (A6-R-4). The signal SOURCE is swappable: today it is INERT (nothing
 * emits in production), so the live-refresh is dormant until merge-back points {@link emitTaskSignal}
 * at the A11 `task.updated` SSE (`/v1/me/events`, data-only) — the same seam, a different source, no
 * change to consumers. (Deliberately NOT the P6 notification feed: a task-refresh signal must not
 * pollute the bell.) Tests drive {@link emitTaskSignal} directly to exercise the hook + refetch.
 */

/** One task.updated signal — the trigger to re-read, never the state itself. */
export interface TaskSignal {
  /** A monotonic signal id for advance-only dedup (a re-seen id never re-fires). */
  id: string;
  /** The task this transition concerns; `null` = a non-targeted signal. */
  taskId: string | null;
}

type Listener = (signal: TaskSignal) => void;
const listeners = new Set<Listener>();

/**
 * Emit a task.updated signal to every subscriber. INERT in production until merge-back wires the
 * `/v1/me/events` SSE reader to call this on each `task.updated` frame; tests call it directly.
 */
export function emitTaskSignal(signal: TaskSignal): void {
  for (const listener of listeners) listener(signal);
}

/**
 * Subscribe a surface to task.updated signals. The handler is a TRIGGER TO REFETCH the durable
 * store (never trust a pushed state); signals dedupe by advance-only id. Returns nothing — the
 * subscription lives for the component's lifetime.
 */
export function useTaskSignal(onSignal: (signal: TaskSignal) => void): void {
  const handler = useRef(onSignal);
  handler.current = onSignal;
  const lastId = useRef<string | null>(null);
  useEffect(() => {
    const listener: Listener = (signal) => {
      if (signal.id === lastId.current) return; // advance-only dedup
      lastId.current = signal.id;
      handler.current(signal);
    };
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
}
