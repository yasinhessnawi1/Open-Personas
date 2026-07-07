/**
 * Spec A6 (W8) — the task.updated consumption seam: the hook's contract, against a MOCK source.
 *
 * - **Advance-only dedup** — a re-seen signal id never re-fires.
 * - **Targeted refetch** — a consumer that filters by taskId only reacts to its own task.
 * - **Unsubscribe** — a signal after unmount reaches no one (no leak).
 * The refetch-not-trust property is structural (consumers call a refetch, never apply pushed state);
 * this exercises the trigger mechanics the surfaces depend on.
 */
import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { emitTaskSignal, useTaskSignal } from "./task-signal";

function Probe({
  onSignal,
}: {
  onSignal: (s: { id: string; taskId: string | null }) => void;
}) {
  useTaskSignal(onSignal);
  return null;
}

describe("useTaskSignal", () => {
  it("fires once per new id and dedups a re-seen id (advance-only)", () => {
    const spy = vi.fn();
    render(<Probe onSignal={spy} />);

    act(() => emitTaskSignal({ id: "s1", taskId: "t1" }));
    act(() => emitTaskSignal({ id: "s1", taskId: "t1" })); // same id → deduped
    act(() => emitTaskSignal({ id: "s2", taskId: "t2" }));

    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy).toHaveBeenNthCalledWith(1, { id: "s1", taskId: "t1" });
    expect(spy).toHaveBeenNthCalledWith(2, { id: "s2", taskId: "t2" });
  });

  it("lets a consumer refetch only its own task (targeted)", () => {
    const refetch = vi.fn();
    render(
      <Probe
        onSignal={(s) => {
          if (s.taskId === "mine") refetch();
        }}
      />,
    );
    act(() => emitTaskSignal({ id: "a", taskId: "other" }));
    expect(refetch).not.toHaveBeenCalled();
    act(() => emitTaskSignal({ id: "b", taskId: "mine" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("unsubscribes on unmount (no leak)", () => {
    const spy = vi.fn();
    const { unmount } = render(<Probe onSignal={spy} />);
    unmount();
    act(() => emitTaskSignal({ id: "z", taskId: "t" }));
    expect(spy).not.toHaveBeenCalled();
  });
});
