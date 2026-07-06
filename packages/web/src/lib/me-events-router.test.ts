import { describe, expect, it, vi } from "vitest";
import { createChannelRouter, parseCursor } from "@/lib/me-events-router";
import type { RawSSEEvent } from "@/lib/sse";

function ready(epoch: string, seq: number): RawSSEEvent {
  return {
    event: "ready",
    data: JSON.stringify({ v: 1, type: "ready", epoch, latest_seq: seq }),
  };
}
function resync(
  epoch: string,
  seq: number,
  reason = "epoch_changed",
): RawSSEEvent {
  return {
    event: "resync",
    data: JSON.stringify({
      v: 1,
      type: "resync",
      epoch,
      latest_seq: seq,
      reason,
    }),
  };
}
function notif(id: string, kind = "run_terminal", ref = "r1"): RawSSEEvent {
  return {
    event: "notification.created",
    id,
    data: JSON.stringify({
      v: 1,
      type: "notification.created",
      kind,
      ref_id: ref,
    }),
  };
}

describe("parseCursor", () => {
  it("splits on the last colon", () => {
    expect(parseCursor("9f3ac1:42")).toEqual({ epoch: "9f3ac1", seq: 42 });
  });
  it("rejects malformed ids", () => {
    expect(parseCursor(undefined)).toBeNull();
    expect(parseCursor("nocolon")).toBeNull();
    expect(parseCursor("epoch:notanint")).toBeNull();
    expect(parseCursor(":5")).toBeNull();
  });
});

describe("createChannelRouter", () => {
  it("ready sets the resume baseline without dispatching", () => {
    const dispatch = vi.fn();
    const r = createChannelRouter(dispatch);
    r.handle(ready("e1", 7));
    expect(dispatch).not.toHaveBeenCalled();
    expect(r.lastEventId()).toBe("e1:7");
  });

  it("dispatches a data event and advances the cursor", () => {
    const dispatch = vi.fn();
    const r = createChannelRouter(dispatch);
    r.handle(ready("e1", 0));
    r.handle(notif("e1:1"));
    expect(dispatch).toHaveBeenCalledWith("notification.created", {
      v: 1,
      type: "notification.created",
      kind: "run_terminal",
      ref_id: "r1",
    });
    expect(r.lastEventId()).toBe("e1:1");
  });

  it("dedupes a replayed/duplicate frame by id (A11-D-4)", () => {
    const dispatch = vi.fn();
    const r = createChannelRouter(dispatch);
    r.handle(ready("e1", 0));
    r.handle(notif("e1:1"));
    r.handle(notif("e1:1")); // same id — a replay overlap
    r.handle(notif("e1:1")); // still duplicate
    expect(dispatch).toHaveBeenCalledTimes(1);
  });

  it("accepts a strictly-newer seq, drops an older one", () => {
    const dispatch = vi.fn();
    const r = createChannelRouter(dispatch);
    r.handle(ready("e1", 5));
    r.handle(notif("e1:5")); // == baseline → duplicate, dropped
    r.handle(notif("e1:6")); // newer → dispatched
    r.handle(notif("e1:4")); // older → dropped
    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(r.lastEventId()).toBe("e1:6");
  });

  it("resync dispatches a full-refetch signal and re-baselines", () => {
    const dispatch = vi.fn();
    const r = createChannelRouter(dispatch);
    r.handle(ready("e1", 3));
    r.handle(resync("e2", 0)); // a new epoch (server restarted)
    expect(dispatch).toHaveBeenCalledWith("resync", {});
    expect(r.lastEventId()).toBe("e2:0");
    // After a resync to a new epoch, a same-seq frame from the NEW epoch is fresh.
    r.handle(notif("e2:1"));
    expect(dispatch).toHaveBeenCalledWith(
      "notification.created",
      expect.objectContaining({ kind: "run_terminal" }),
    );
  });

  it("ignores unknown event names (heartbeats / future types)", () => {
    const dispatch = vi.fn();
    const r = createChannelRouter(dispatch);
    r.handle(ready("e1", 0));
    r.handle({ event: "something.new", id: "e1:1", data: "{}" });
    expect(dispatch).not.toHaveBeenCalled();
  });
});
