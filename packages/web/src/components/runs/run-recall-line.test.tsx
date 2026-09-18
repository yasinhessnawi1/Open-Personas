/**
 * part3 F10: the run header names the typed memory the run read.
 *
 * Both cases are built by the real reductions rather than by a hand written view: a
 * watched run comes through `runViewFromEvents`, a reopened one through
 * `runViewFromSnapshot` over the persisted step notes. A test that hands the component a
 * literal `recall` array would pass with the whole wiring missing, which is exactly the
 * shape of defect this fixes.
 */
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { RunStatusResponse } from "@/lib/api";
import { runViewFromEvents, runViewFromSnapshot } from "@/lib/run";
import type { RunEvent } from "@/lib/sse-types";

import { RunRecallLine } from "./run-recall-line";

const T = "2026-09-18T00:00:00Z";

const liveEvents: RunEvent[] = [
  { type: "started", step: -1, data: { task: "summarise" }, timestamp: T },
  ...(
    [
      ["identity", 0],
      ["self_facts", 1],
      ["worldview", 0],
      ["episodic", 2],
    ] as const
  ).map(
    ([store, count]) =>
      ({
        type: "memory_recall",
        step: -1,
        data: { store, count },
        timestamp: T,
      }) as RunEvent,
  ),
  { type: "reasoning", step: 0, data: { content: "thinking" }, timestamp: T },
];

const reopened = {
  id: "r1",
  persona_id: "astrid",
  task: "summarise",
  status: "completed",
  output: "All done.",
  steps: [
    {
      type: "reasoning",
      content: "thinking",
      notes: [
        { kind: "memory_recall", store: "identity", count: 0 },
        { kind: "memory_recall", store: "self_facts", count: 1 },
        { kind: "memory_recall", store: "worldview", count: 0 },
        { kind: "memory_recall", store: "episodic", count: 2 },
      ],
    },
  ],
} as unknown as RunStatusResponse;

function mount(recall: { store: string; count?: number }[] | undefined) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <RunRecallLine recall={recall ?? []} />
    </NextIntlClientProvider>,
  );
}

const line = (c: HTMLElement) =>
  c.querySelector('[data-slot="run-view-recall"]');

describe("RunRecallLine", () => {
  it("names every store the WATCHED run read, with its count", () => {
    const { container } = mount(
      runViewFromEvents(liveEvents, { task: "summarise" }).recall,
    );

    const text = line(container)?.textContent ?? "";
    expect(text).toContain("Recalled from");
    expect(text).toContain("identity (0)");
    expect(text).toContain("self facts (1)");
    expect(text).toContain("episodic (2)");
  });

  it("says the same on a REOPENED run, read off the first step's notes", () => {
    const watched = mount(
      runViewFromEvents(liveEvents, { task: "summarise" }).recall,
    );
    const later = mount(runViewFromSnapshot(reopened).recall);

    expect(line(later.container)?.textContent).toBe(
      line(watched.container)?.textContent,
    );
  });

  it("colours each dot from its store and holds it still", () => {
    const { container } = mount(runViewFromSnapshot(reopened).recall);

    const dots = [...container.querySelectorAll(".v-recall-dot")];
    expect(dots.map((d) => d.getAttribute("data-store"))).toEqual([
      "identity",
      "self_facts",
      "worldview",
      "episodic",
    ]);
    expect(dots.every((d) => d.getAttribute("data-static") === "true")).toBe(
      true,
    );
  });

  it("renders nothing when the run recalled nothing", () => {
    const { container } = mount(undefined);

    expect(line(container)).toBeNull();
  });

  it("prints an unknown store token rather than crashing on it", () => {
    const { container } = mount([{ store: "procedural", count: 3 }]);

    expect(line(container)?.textContent).toContain("procedural (3)");
  });
});
