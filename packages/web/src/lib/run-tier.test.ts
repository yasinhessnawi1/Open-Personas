/**
 * part3 F12: the run header's tier badge says the same thing whether you watched the run
 * or reopened it.
 *
 * The REOPENED half is written first, and deliberately. It is the half that already
 * worked (the record stamps `tier_used` on every step), so it is the reference: the live
 * stream, which used to send no tier frame at all and left the badge blank for the whole
 * run, has to arrive at the same answer from the events.
 *
 * A run that switches tier is the case that decides it. The live stream announces a tier
 * once and then only when it changes, so the header ends on the tier the run is actually
 * on; the reopened header has to read the same, which means the LAST step that names a
 * tier, not the first.
 */
import { describe, expect, it } from "vitest";
import type { RunStatusResponse } from "@/lib/api";
import { runViewFromEvents, runViewFromSnapshot } from "@/lib/run";
import type { RunEvent } from "@/lib/sse-types";

const T = "2026-09-18T00:00:00Z";

const tierEvent = (step: number, tier: string): RunEvent =>
  ({ type: "tier", step, data: { tier }, timestamp: T }) as RunEvent;

/** The persisted shape of a finished run whose steps ran on `tiers`, in order. */
const snapshot = (tiers: string[]): RunStatusResponse =>
  ({
    id: "r1",
    persona_id: "astrid",
    task: "draft a complaint",
    status: "completed",
    output: "done",
    steps: tiers.map((tier, i) => ({
      type: i === tiers.length - 1 ? "final" : "reasoning",
      content: i === tiers.length - 1 ? "done" : "thinking",
      tier_used: tier,
    })),
  }) as unknown as RunStatusResponse;

describe("run tier badge", () => {
  it("reads the tier off a reopened run that stayed on one tier", () => {
    const view = runViewFromSnapshot(snapshot(["frontier", "frontier"]));
    expect(view.tier).toBe("frontier");
    expect(view.steps.map((s) => s.tier)).toEqual(["frontier", "frontier"]);
  });

  it("reads the tier a reopened run ENDED on when its steps switched", () => {
    const view = runViewFromSnapshot(snapshot(["frontier", "frontier", "mid"]));
    expect(view.tier).toBe("mid");
    expect(view.steps.map((s) => s.tier)).toEqual([
      "frontier",
      "frontier",
      "mid",
    ]);
  });

  it("takes the live header tier from the first frame", () => {
    const view = runViewFromEvents(
      [
        { type: "started", step: -1, data: { task: "t" }, timestamp: T },
        tierEvent(0, "frontier"),
        { type: "thinking", step: 0, data: {}, timestamp: T },
      ],
      { task: "fallback" },
    );
    expect(view.tier).toBe("frontier");
  });

  it("updates the live header tier when a later frame changes it", () => {
    const view = runViewFromEvents(
      [
        { type: "started", step: -1, data: { task: "t" }, timestamp: T },
        tierEvent(0, "frontier"),
        { type: "reasoning", step: 0, data: { content: "a" }, timestamp: T },
        { type: "reasoning", step: 1, data: { content: "b" }, timestamp: T },
        tierEvent(2, "mid"),
        { type: "completed", step: 2, data: { output: "done" }, timestamp: T },
      ],
      { task: "fallback" },
    );
    expect(view.tier).toBe("mid");
  });

  it("shows the same tier live and reopened for the same run", () => {
    // One run, told twice: the frames the loop emits for a run whose third step
    // re-grades, and the record that same run leaves behind.
    const live = runViewFromEvents(
      [
        { type: "started", step: -1, data: { task: "t" }, timestamp: T },
        tierEvent(0, "frontier"),
        { type: "reasoning", step: 0, data: { content: "a" }, timestamp: T },
        { type: "reasoning", step: 1, data: { content: "b" }, timestamp: T },
        tierEvent(2, "mid"),
        { type: "completed", step: 2, data: { output: "done" }, timestamp: T },
      ],
      { task: "draft a complaint" },
    );
    const reopened = runViewFromSnapshot(
      snapshot(["frontier", "frontier", "mid"]),
    );
    expect(live.tier).toBe(reopened.tier);
  });
});
