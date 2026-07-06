/**
 * Spec A11 — the pure frame router behind {@link MeEventsProvider} (extracted so the
 * dedupe / resume state machine is unit-testable without React or a live socket).
 *
 * Tracks the resume cursor (`epoch:seq`), dedupes data frames by id (A11-D-4), and
 * turns `ready`/`resync` control frames into cursor updates + a `resync` dispatch
 * (the client's full-refetch signal, A11-D-3). It NEVER interprets payload as state —
 * a data frame becomes a typed dispatch whose consumers refetch.
 */

import type { RawSSEEvent } from "@/lib/sse";

/** The closed client-facing event set (data events + the `resync` control signal). */
export type MeEventType =
  | "notification.created"
  | "message.delivered"
  | "task.updated"
  | "resync";

const DATA_EVENTS: ReadonlySet<string> = new Set([
  "notification.created",
  "message.delivered",
  "task.updated",
]);

export type MeDispatch = (
  type: MeEventType,
  data: Record<string, unknown>,
) => void;

export interface ChannelRouter {
  /** Feed one raw SSE frame; dispatches to consumers as warranted. */
  handle: (frame: RawSSEEvent) => void;
  /** The `Last-Event-ID` to send on the next reconnect (null before any baseline). */
  lastEventId: () => string | null;
}

/** Parse an `epoch:seq` id; null if malformed. Splits on the LAST colon (epoch is opaque). */
export function parseCursor(
  id: string | undefined,
): { epoch: string; seq: number } | null {
  if (!id) return null;
  const at = id.lastIndexOf(":");
  if (at <= 0) return null;
  const seq = Number(id.slice(at + 1));
  if (!Number.isFinite(seq)) return null;
  return { epoch: id.slice(0, at), seq };
}

function parseData(frame: RawSSEEvent): Record<string, unknown> {
  try {
    return JSON.parse(frame.data) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export function createChannelRouter(dispatch: MeDispatch): ChannelRouter {
  let cursor: string | null = null;
  let curEpoch: string | null = null;
  let curSeq = -1;

  const baseline = (frame: RawSSEEvent): void => {
    const b = parseData(frame);
    curEpoch = String(b.epoch ?? "");
    curSeq = Number(b.latest_seq ?? -1);
    cursor = `${curEpoch}:${curSeq}`;
  };

  return {
    lastEventId: () => cursor,
    handle: (frame: RawSSEEvent): void => {
      if (frame.event === "ready") {
        baseline(frame); // adopt the server's {epoch, latest_seq} — no dispatch
        return;
      }
      if (frame.event === "resync") {
        baseline(frame);
        dispatch("resync", {}); // the server couldn't honour our cursor → full refetch
        return;
      }
      if (!DATA_EVENTS.has(frame.event)) return; // unknown/heartbeat — ignore
      const c = parseCursor(frame.id);
      if (c) {
        if (c.epoch === curEpoch && c.seq <= curSeq) return; // duplicate / replay (A11-D-4)
        curEpoch = c.epoch;
        curSeq = c.seq;
        cursor = frame.id ?? cursor;
      }
      dispatch(frame.event as MeEventType, parseData(frame));
    },
  };
}
