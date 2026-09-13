/**
 * Spec F4 T04 — RunEvent → `OutputContent[]` normaliser (view-time
 * derivation per D-F4-X-output-derivation-shape).
 *
 * Peels the D-09-1 RunEvent `.data` envelope and otherwise mirrors the
 * chat-side normaliser (`./chat-output.ts`) by sharing dispatch logic
 * via `./_classify.ts`. Two transports, ONE classification policy,
 * IDENTICAL OutputContent emitted — the cross-surface consistency
 * guarantee (criterion 6) is structural.
 *
 * Per-event dispatch:
 *   - `tool_calling`: emit one `working` per recognized capability tool.
 *   - `tool_result`: `is_error` → `failure`; structured `produced_files`
 *     → classify each; otherwise → `result-block`.
 *   - `error`: top-level RunEvent.error (not a tool failure) → `failure`
 *     with `operation = "run"`. This is the *run-level* failure shape;
 *     tool-level failures come through `tool_result.is_error`.
 *   - Every other event type (started / tier / thinking / asking_user /
 *     user_responded / reasoning / completed / cancelled / max_steps /
 *     finished): returns [] — those are timeline state consumed by
 *     `runViewFromEvents`, not capability output.
 *
 * `<StepCard>` (T11) reads `step.outputs[]` from the derived view at
 * `packages/web/src/lib/run.ts`; the per-step accumulation logic lives
 * there. This module is the single-event projection used both by that
 * reduction and by any direct caller (e.g. a hypothetical run-event
 * inspector surface in F5).
 */

import type { OutputContent } from "@/lib/api/output-content";
import type { RunEvent } from "@/lib/sse-types";

import { projectToolCalling, projectToolResult } from "./_classify";

export function runEventToOutputContent(event: RunEvent): OutputContent[] {
  switch (event.type) {
    case "tool_calling":
      return projectToolCalling(event.data.tool_calls);
    case "tool_result":
      return projectToolResult(event.data);
    case "error":
      return [
        {
          kind: "failure",
          operation: "run",
          error_message: event.data.message,
        },
      ];
    // Timeline-state events handled by runViewFromEvents at lib/run.ts;
    // they do not contribute capability output.
    case "started":
    case "tier":
    case "thinking":
    case "memory_recall":
    // P2: the activity contract is live-state for runViewFromEvents (step.activities),
    // not capability output — it never contributes to step.outputs.
    case "activity_start":
    case "activity_end":
    case "asking_user":
    case "user_responded":
    case "reasoning":
    // Spec W1 (T10): the guard notes are step-trace state for runViewFromEvents
    // (step.notes), never capability output.
    case "call_skipped":
    case "context_pruned":
    case "completed":
    case "cancelled":
    case "max_steps":
    case "finished":
      return [];
  }
}
