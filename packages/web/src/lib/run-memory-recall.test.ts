/**
 * part3 F10: the run viewer says which typed memory the persona read for the run, both
 * while it is watched (the run level `memory_recall` events) and when it is reopened
 * (the `memory_recall` notes the loop records on the run's first step).
 *
 * The REOPENED half is written first and deliberately: R9-157 is the lesson that a
 * disclosure proven only on the live stream is a disclosure that exists only for whoever
 * happened to be watching, and a multi day task is the surface nobody watches.
 */
import { describe, expect, it } from "vitest";
import type { RunStatusResponse } from "@/lib/api";
import { runViewFromEvents, runViewFromSnapshot } from "@/lib/run";
import type { RunEvent } from "@/lib/sse-types";

const T = "2026-09-18T00:00:00Z";

const recallEvent = (store: string, count: number): RunEvent =>
  ({
    type: "memory_recall",
    step: -1,
    data: { store, count },
    timestamp: T,
  }) as RunEvent;

const liveEvents: RunEvent[] = [
  { type: "started", step: -1, data: { task: "summarise" }, timestamp: T },
  recallEvent("identity", 0),
  recallEvent("self_facts", 1),
  recallEvent("worldview", 0),
  recallEvent("episodic", 2),
  { type: "reasoning", step: 0, data: { content: "thinking" }, timestamp: T },
  { type: "completed", step: 1, data: { output: "All done." }, timestamp: T },
];

const RECALL = [
  { store: "identity", count: 0 },
  { store: "self_facts", count: 1 },
  { store: "worldview", count: 0 },
  { store: "episodic", count: 2 },
];

const snapshot = (firstStepNotes: unknown): RunStatusResponse =>
  ({
    id: "r1",
    persona_id: "astrid",
    task: "summarise",
    status: "completed",
    output: "All done.",
    steps: [
      { type: "reasoning", content: "thinking", notes: firstStepNotes },
      { type: "final", content: "All done.", notes: [] },
    ],
  }) as unknown as RunStatusResponse;

const persistedNotes = [
  { kind: "memory_recall", store: "identity", count: 0 },
  { kind: "memory_recall", store: "self_facts", count: 1 },
  { kind: "memory_recall", store: "worldview", count: 0 },
  { kind: "memory_recall", store: "episodic", count: 2 },
];

describe("runViewFromSnapshot: the memory the run read (REOPENED)", () => {
  it("derives the run's recall from the first persisted step's notes", () => {
    expect(runViewFromSnapshot(snapshot(persistedNotes)).recall).toEqual(
      RECALL,
    );
  });

  it("says nothing on a run recorded before the note existed", () => {
    expect(runViewFromSnapshot(snapshot(undefined)).recall).toBeUndefined();
    expect(runViewFromSnapshot(snapshot([])).recall).toBeUndefined();
  });

  it("keeps the recall off the steps, which is where it could never render", () => {
    // `runViewFromEvents` drops every step below zero, and the recall is emitted at
    // step -1. On the header it is reachable; on a step it is not.
    const view = runViewFromSnapshot(snapshot(persistedNotes));
    expect(view.steps[0].notes).toBeUndefined();
  });

  it("leaves a guard note on the same step alone", () => {
    const view = runViewFromSnapshot(
      snapshot([
        { kind: "call_skipped", tool: "web_search", guard: "cached_read" },
        ...persistedNotes,
      ]),
    );

    expect(view.steps[0].notes).toEqual([
      { kind: "call_skipped", tool: "web_search" },
    ]);
    expect(view.recall).toEqual(RECALL);
  });

  it("reads the same as the run that was watched", () => {
    const live = runViewFromEvents(liveEvents, { task: "summarise" });
    expect(runViewFromSnapshot(snapshot(persistedNotes)).recall).toEqual(
      live.recall,
    );
  });

  it("carries the recall through the event-log snapshot shape too", () => {
    // A run that is still running (or crashed) is served as its event log, not as steps.
    const running = {
      id: "r1",
      persona_id: "astrid",
      task: "summarise",
      status: "running",
      steps: liveEvents,
    } as unknown as RunStatusResponse;

    expect(runViewFromSnapshot(running).recall).toEqual(RECALL);
  });
});

describe("runViewFromEvents: the memory the run read (LIVE)", () => {
  it("puts the recall on the run header, one entry per typed store", () => {
    const view = runViewFromEvents(liveEvents, { task: "summarise" });

    expect(view.recall).toEqual(RECALL);
    expect(view.steps.map((s) => s.step)).toEqual([0, 1]);
  });

  it("is idempotent when the stream replays from the start of the queue", () => {
    const view = runViewFromEvents([...liveEvents, ...liveEvents], {
      task: "summarise",
    });

    expect(view.recall).toEqual(RECALL);
  });

  it("says nothing on a run that emitted no recall", () => {
    const view = runViewFromEvents(
      liveEvents.filter((e) => e.type !== "memory_recall"),
      { task: "summarise" },
    );

    expect(view.recall).toBeUndefined();
  });
});
