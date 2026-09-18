import type { ToolEntry } from "@/components/chat/tool-call-card";
import type { ActivityView } from "@/lib/activity";
import { reduceActivityEnd, reduceActivityStart } from "@/lib/activity";
import type { RunStatusResponse } from "@/lib/api";
import type { OutputContent } from "@/lib/api/output-content";
import {
  projectToolCalling,
  projectToolResult,
} from "@/lib/normalisers/_classify";
import type {
  ArtifactRef,
  QuestionOption,
  RunEvent,
  ToolResultData,
} from "@/lib/sse-types";
import { warnUnhandledSseEvent } from "@/lib/sse-types";

// Normalised run-viewer model (T07). Both the live `RunEvent` SSE stream and the
// persisted `GET /runs/:id` `steps[]` reduce into one {@link RunStep} shape so the
// timeline has a single render path.
//
// ⚠ `runs.steps` has TWO shapes depending on run state (verified against
// packages/api/.../background/run_worker.py):
//   - while RUNNING / on a crash-ERROR: the event-log snapshot — a list of
//     `RunEvent.model_dump` dicts `{type, step, data, timestamp}` (same shape as
//     the SSE frames).
//   - on COMPLETED / CANCELLED / MAX_STEPS: `Step.model_dump` dicts
//     `{type, tool_calls, results, question, user_answer, content, tier_used, …}`.
// {@link runViewFromSnapshot} discriminates per-entry and reduces accordingly.

/** Mirrors persona_runtime `RunStatus` plus the API's in-flight `"running"`. */
export type RunStatus =
  | "running"
  | "awaiting_user"
  | "completed"
  | "cancelled"
  | "max_steps_reached"
  | "error";

/**
 * Spec W1 (D-W1-34): `awaiting_user` is stopped but NOT terminal. The leg ended on a
 * question, so nothing more lands on this run, yet the work is not over: the answer arrives
 * through the task's reply route and continues in a new run. Terminal drives "this is
 * finished" affordances, and this run is not finished, it is waiting for a person.
 */
const TERMINAL: ReadonlySet<RunStatus> = new Set([
  "completed",
  "cancelled",
  "max_steps_reached",
  "error",
]);

export function isTerminal(status: string): boolean {
  return TERMINAL.has(status as RunStatus);
}

/** One plan-act-reflect cycle, normalised for display. */
export interface RunStep {
  step: number;
  thinking: boolean;
  tools: ToolEntry[];
  /**
   * Spec F4 T04 (D-F4-X-output-derivation-shape): rich-output renderer
   * inputs derived view-time from this step's `tool_calling` +
   * `tool_result` events. Populated by {@link runViewFromEvents}; consumed
   * by `<StepCard>` (T11) through the renderer dispatcher (T09).
   *
   * Lifecycle:
   *   - `tool_calling` SETS the array to one `working` per recognized
   *     capability tool (image_gen / code_exec / doc_gen).
   *   - `tool_result` REPLACES the matching `working` (by tool name) with
   *     the projected outputs: `failure` on is_error, classified
   *     produced files on structured payload, or `result-block` on a
   *     plain-stdout result. Multiple produced files expand the slot.
   *   - Unrecognized capability tools (web_search, file_*, …) emit
   *     NOTHING to this array — they surface via the existing tool-card
   *     path. The F4 output surface is for rich outputs only.
   *
   * Top-level RunEvent `error` does NOT push here — it surfaces via the
   * existing {@link error} field. Step-card has its own error display
   * surface; doubling up would be noise.
   */
  outputs: OutputContent[];
  /**
   * P2 — the live "using <X>…" activity states for this step, a SEPARATE channel from
   * {@link tools} (the card). `activity_start` opens an entry; `activity_end` resolves
   * it by `activityId`. Kept apart from `tools`/`outputs` so a call renders ONE card
   * (from `tool_result`) plus a transient activity state — never two cards (P2-D-3
   * no-double-render). Absent on pre-P2 / historical runs.
   */
  activities?: ActivityView[];
  reasoning?: string;
  /**
   * Spec W1 (T10): what the run's deterministic guards did on this step, rendered as
   * muted notes under the tool cards. A guard that fires is a call the model asked for
   * and did not get, or output it can no longer read in full; leaving that invisible
   * makes a run look idle or forgetful for no stated reason.
   *
   * Filled by BOTH reductions (R9-157): the live `call_skipped` / `context_pruned`
   * events, and the persisted `Step.notes` a reopened run is rebuilt from. It used to be
   * the live path only, which made a working guard invisible to everyone who was not
   * watching the run happen.
   */
  notes?: RunStepNote[];
  question?: string;
  /** Spec 21 (D-21-9): the 3+1 options when the ask carries them; else absent. */
  options?: QuestionOption[];
  allowFreeForm?: boolean;
  answered: boolean;
  final?: string;
  maxSteps?: string;
  error?: string;
  tier?: string;
}

/**
 * One line of guard activity on a step (Spec W1, T10). Structured rather than a string:
 * the copy lives in the message catalogue, and this module holds no user-facing text.
 */
export type RunStepNote =
  | { kind: "call_skipped"; tool: string }
  | { kind: "context_pruned" }
  /**
   * Spec C0: the run's conclusion went out as a message the persona started. Lives on the
   * LAST step (the event is run-level, `step: -1`, and the record stamps the final step),
   * with the conversation it landed in so the viewer can link to it.
   */
  | { kind: "persona_originated"; conversationId?: string };

/**
 * One typed memory store the run read for its initial context. Same shape the chat
 * surface uses for its "Recalling from <store> memory" state, so the two surfaces speak
 * one vocabulary rather than two that drift.
 */
export interface RunRecall {
  store: string;
  count?: number;
}

/** The whole run, as the viewer renders it. */
export interface RunView {
  task: string;
  status: RunStatus;
  tier?: string;
  /**
   * part3 F10: the typed memory this run read before its first step, in the order the
   * loop consulted the stores. It belongs to the RUN, not to a step: the loop emits it
   * at `step: -1`, and the step list drops everything below zero, so a step-level home
   * for it could never render. Absent on a run that recalled nothing.
   */
  recall?: RunRecall[];
  steps: RunStep[];
  output?: string;
  error?: string;
}

function emptyStep(step: number): RunStep {
  return { step, thinking: false, tools: [], outputs: [], answered: false };
}

// ----- RunEvent reduction (live stream + the running/error snapshot) -----

/**
 * Reduce an ordered `RunEvent` list into a {@link RunView}. Keyed by step index,
 * so replaying overlapping events (SSE replays from the start of the buffered
 * queue; reconnects re-seed) is idempotent — `tool_calling` SETS the step's tool
 * list rather than appending.
 */
export function runViewFromEvents(
  events: readonly RunEvent[],
  base: { task: string },
): RunView {
  const map = new Map<number, RunStep>();
  // Keyed by store so a replayed stream cannot list `episodic` twice; insertion order is
  // the order the loop consulted the stores, which is the order worth showing.
  const recall = new Map<string, RunRecall>();
  let tier: string | undefined;
  let status: RunStatus = "running";
  let task = base.task;
  let output: string | undefined;
  let error: string | undefined;

  const ensure = (s: number): RunStep => {
    let st = map.get(s);
    if (!st) {
      st = emptyStep(s);
      map.set(s, st);
    }
    return st;
  };

  for (const ev of events) {
    switch (ev.type) {
      case "started":
        task = ev.data.task;
        break;
      case "tier":
        tier = ev.data.tier;
        break;
      case "thinking":
        ensure(ev.step).thinking = true;
        break;
      case "memory_recall":
        // part3 F10: the run read this typed store for its initial context. It is a RUN
        // level fact (`step: -1`, before any step exists), so it folds onto the header
        // next to the tier; a step-level home would be filtered out and never seen. The
        // comment here used to say the agentic loop did not emit this, which stopped
        // being true when the loop started emitting it, and a stale "not emitted yet" is
        // how a wired feature stays unwired.
        recall.set(ev.data.store, {
          store: ev.data.store,
          count: ev.data.count,
        });
        break;
      case "tool_calling": {
        const st = ensure(ev.step);
        st.thinking = false;
        st.tools = ev.data.tool_calls.map((c) => ({
          toolName: c.name,
          args: c.args,
          pending: true,
        }));
        // F4 T04: seed step.outputs with one `working` per recognized
        // capability tool. Unrecognized tools contribute nothing — their
        // result surfaces through st.tools' tool-card path.
        st.outputs = projectToolCalling(ev.data.tool_calls);
        break;
      }
      case "activity_start": {
        // P2: open the live "using <X>…" state on this step — a SEPARATE channel from
        // st.tools (the card stays sourced from tool_result during keep-both, P2-D-3).
        // Idempotent on replay (dedup by activity_id).
        const st = ensure(ev.step);
        st.thinking = false;
        st.activities = reduceActivityStart(st.activities, ev.data);
        break;
      }
      case "activity_end": {
        // P2: resolve the matching live state by activity_id (no-op if no start seen).
        const st = ensure(ev.step);
        st.activities = reduceActivityEnd(st.activities, ev.data);
        break;
      }
      case "tool_result": {
        const st = ensure(ev.step);
        for (let i = st.tools.length - 1; i >= 0; i--) {
          if (
            st.tools[i].toolName === ev.data.tool_name &&
            st.tools[i].pending
          ) {
            st.tools[i] = {
              ...st.tools[i],
              result: ev.data.content,
              isError: ev.data.is_error,
              pending: false,
            };
            break;
          }
        }
        // F4 T04: replace the matching pending `working` in st.outputs with
        // the projected result (failure / classified produced files /
        // result-block). Mirror the tool_call matching: search backward by
        // label === tool_name; the last unresolved working wins (handles
        // parallel calls of the same capability tool).
        for (let i = st.outputs.length - 1; i >= 0; i--) {
          const item = st.outputs[i];
          if (item.kind === "working" && item.label === ev.data.tool_name) {
            st.outputs.splice(i, 1, ...projectToolResult(ev.data));
            break;
          }
        }
        break;
      }
      case "asking_user": {
        const st = ensure(ev.step);
        st.thinking = false;
        st.question = ev.data.question;
        // Spec 21 (D-21-9): carry the 3+1 options when present (additive).
        st.options = ev.data.options;
        st.allowFreeForm = ev.data.allow_free_form;
        break;
      }
      case "user_responded":
        ensure(ev.step).answered = true;
        break;
      case "reasoning": {
        const st = ensure(ev.step);
        st.thinking = false;
        st.reasoning = ev.data.content;
        break;
      }
      case "call_skipped": {
        // Spec W1 (D-W1-11): the ledger answered this call. Appended, not set: a step can
        // batch several calls and skip more than one of them.
        const st = ensure(ev.step);
        st.notes = [
          ...(st.notes ?? []),
          { kind: "call_skipped", tool: ev.data.tool },
        ];
        break;
      }
      case "context_pruned": {
        // Spec W1 (D-W1-13): older tool output was trimmed at this step's boundary.
        const st = ensure(ev.step);
        st.notes = [...(st.notes ?? []), { kind: "context_pruned" }];
        break;
      }
      case "persona_originated": {
        // Spec C0: the persona sent the conclusion on as a message. The event is
        // run-level (`step: -1`, which the step list drops), so it lands on the last
        // step, which is where the durable record stamps it too; the two paths then
        // put the note in the same place. Idempotent on replay (one note per run).
        const last = Math.max(0, ...map.keys());
        const st = ensure(last);
        const note: RunStepNote = {
          kind: "persona_originated",
          conversationId: ev.data.conversation_id || undefined,
        };
        st.notes = [
          ...(st.notes ?? []).filter((n) => n.kind !== "persona_originated"),
          note,
        ];
        break;
      }
      case "completed": {
        const st = ensure(ev.step);
        st.thinking = false;
        st.final = ev.data.output;
        output = ev.data.output;
        status = "completed";
        break;
      }
      case "max_steps": {
        const st = ensure(ev.step);
        st.thinking = false;
        st.maxSteps = ev.data.summary;
        output = ev.data.summary;
        status = "max_steps_reached";
        break;
      }
      case "cancelled":
        status = "cancelled";
        break;
      case "error": {
        const st = ensure(ev.step);
        st.error = ev.data.message;
        error = ev.data.message;
        status = "error";
        break;
      }
      case "finished":
        // The authoritative terminal status (str(RunStatus)).
        status = (ev.data.status as RunStatus) ?? status;
        break;
      default:
        // An unhandled run event is dropped here. Loud, not silent: the durable render
        // (the persisted record, present on next open) is the backstop, so this is a
        // missed live render, not data loss, and the warning is what turns it into a
        // finding rather than a mystery.
        warnUnhandledSseEvent("run", (ev as { type: string }).type);
        break;
    }
  }

  const steps = [...map.values()]
    .filter((s) => s.step >= 0)
    .sort((a, b) => a.step - b.step);
  return {
    task,
    status,
    tier,
    recall: recall.size > 0 ? [...recall.values()] : undefined,
    steps,
    output,
    error,
  };
}

// ----- persisted Step reduction (the terminal-final snapshot shape) -----

interface PersistedToolCall {
  name: string;
  call_id?: string;
  args?: Record<string, unknown>;
}
interface PersistedToolResult {
  tool_name: string;
  content: string;
  call_id?: string;
  is_error?: boolean;
  /**
   * Spec 28 byte-outputs, as the durable record carries them. Present on a stored run
   * exactly as on the live frame, which is what makes part3 F2 fixable at all.
   */
  artifacts?: ArtifactRef[];
  /**
   * The tool's structured payload. `produced_files` lives in here on the record, and is
   * lifted onto the frame's top level by `RunEvent.tool_result`; this reader does the same
   * lift so a reopened run and a watched one classify identically.
   */
  data?: Record<string, unknown> | null;
  /**
   * R9-163: the tool's own report that it cut this result. The durable Step round-trips
   * the whole ToolResult, so the record has it; the reopen path forwards it exactly as
   * `RunEvent.tool_result` does on the live frame.
   */
  truncated?: boolean;
}
/**
 * One `Step.notes` entry as the durable record carries it (R9-157). Every field is
 * optional here because the record is data, not a promise: a run written before this
 * field existed has no `notes` at all, and a note whose shape we cannot read is dropped
 * rather than rendered half-stated.
 */
interface PersistedStepNote {
  kind?: string;
  tool?: string;
  guard?: string;
  conversation_id?: string;
  store?: string;
  count?: number;
}
interface PersistedStep {
  type: string;
  tool_calls?: PersistedToolCall[];
  results?: PersistedToolResult[];
  question?: string | null;
  user_answer?: string | null;
  content?: string | null;
  notes?: PersistedStepNote[] | null;
  tier_used?: string | null;
}

/**
 * Read the notes off a persisted step, so a REOPENED run says what the run's guards did
 * (R9-157) and whether its persona spoke first (Spec C0). The live stream reduces the
 * `call_skipped` / `context_pruned` / `persona_originated` events into the same
 * {@link RunStep.notes}; this is the other half, and without it the disclosure exists
 * only while someone is watching.
 *
 * `memory_recall` notes are deliberately not among the kinds read here: the recall is a
 * run level fact and {@link persistedRecall} lifts it to the header, the same place the
 * live stream puts it.
 */
function persistedNotes(
  raw: PersistedStepNote[] | null | undefined,
): RunStepNote[] {
  const notes: RunStepNote[] = [];
  for (const note of raw ?? []) {
    if (note.kind === "call_skipped" && note.tool) {
      notes.push({ kind: "call_skipped", tool: note.tool });
    } else if (note.kind === "context_pruned") {
      notes.push({ kind: "context_pruned" });
    } else if (note.kind === "persona_originated") {
      // Spec C0: the stamp the worker writes once the message has landed; the
      // conversation id is what lets a reopened run open the chat it started.
      notes.push({
        kind: "persona_originated",
        conversationId: note.conversation_id || undefined,
      });
    }
  }
  return notes;
}

/**
 * Read the run's memory recall off the FIRST persisted step (part3 F10). The loop records
 * it there because step 0 is the step that consumed the context the recall fed, and the
 * viewer lifts it back to the run header, which is where the live stream puts it. Without
 * this half the recall exists only while someone watches the run, which on a task that
 * runs for days is nobody.
 */
function persistedRecall(
  first: PersistedStep | undefined,
): RunRecall[] | undefined {
  const recall: RunRecall[] = [];
  for (const note of first?.notes ?? []) {
    if (note.kind === "memory_recall" && note.store) {
      recall.push({ store: note.store, count: note.count });
    }
  }
  return recall.length > 0 ? recall : undefined;
}

function isRunEventDict(x: unknown): x is RunEvent {
  return typeof x === "object" && x !== null && "timestamp" in x;
}

/**
 * Rebuild the `tool_result` frame payload from a persisted result, so the reopen path and
 * the live path hand {@link projectToolResult} the same shape (part3 F2).
 *
 * `produced_files` is lifted out of `data` exactly as `RunEvent.tool_result` lifts it, and
 * an empty list is omitted rather than passed as `[]`, because the classifier treats
 * absence as "fall back" and an empty array would read as "there were none". `truncated`
 * is forwarded the same way (R9-163): set only when the record says the tool cut the
 * result, so a reopened run shows the same indicator a watched one did.
 */
function persistedResultAsFrame(r: PersistedToolResult): ToolResultData {
  const frame: ToolResultData = {
    tool_name: r.tool_name,
    is_error: r.is_error ?? false,
    content: r.content,
  };
  if (r.artifacts !== undefined && r.artifacts.length > 0) {
    frame.artifacts = r.artifacts;
  }
  const pf = r.data?.produced_files;
  if (Array.isArray(pf) && pf.length > 0) {
    frame.produced_files = pf as ToolResultData["produced_files"];
  }
  if (r.truncated === true) {
    frame.truncated = true;
  }
  return frame;
}

function stepToRunStep(s: PersistedStep, index: number): RunStep {
  const out = emptyStep(index);
  out.tier = s.tier_used ?? undefined;
  // Every step type can carry notes: the ledger answers a call on a tool_call step, and
  // the pruner trims at the boundary of whatever step just ran.
  const notes = persistedNotes(s.notes);
  if (notes.length > 0) out.notes = notes;
  switch (s.type) {
    case "tool_call": {
      const calls = s.tool_calls ?? [];
      const results = s.results ?? [];
      out.tools = calls.map((c) => {
        const r =
          (c.call_id
            ? results.find((x) => x.call_id && x.call_id === c.call_id)
            : undefined) ?? results.find((x) => x.tool_name === c.name);
        return {
          toolName: c.name,
          args: c.args,
          result: r?.content,
          isError: r?.is_error,
          pending: r === undefined,
        };
      });
      // part3 F2: reopen a run and every generated image, chart and document used to
      // come back as a paragraph of text. The live stream classifies each result into
      // rich `outputs`; this path built `tools` and left `outputs` empty, so the run you
      // watched and the run you reopened showed different things, and the difference was
      // the whole point of generating a file.
      //
      // The record had the data all along. Artifacts persist on the result, and
      // `produced_files` persists inside its `data` — the same two places
      // `RunEvent.tool_result` reads when it builds the live frame. Rebuilding that exact
      // payload here is what makes the two paths agree by construction rather than by
      // two normalisers being kept in step by hand.
      out.outputs = results.flatMap((r) =>
        projectToolResult(persistedResultAsFrame(r)),
      );
      break;
    }
    case "ask_user":
      out.question = s.question ?? undefined;
      out.answered = s.user_answer != null;
      break;
    case "final":
      out.final = s.content ?? undefined;
      break;
    case "reasoning":
      out.reasoning = s.content ?? undefined;
      break;
    case "error":
      out.error = s.content ?? undefined;
      break;
  }
  return out;
}

/**
 * Build a {@link RunView} from a `GET /runs/:id` response, handling both
 * `steps[]` shapes. The response's top-level `status`/`output`/`error` are
 * authoritative over anything derived from the steps.
 */
export function runViewFromSnapshot(snap: RunStatusResponse): RunView {
  const raw = snap.steps ?? [];
  const status = snap.status as RunStatus;

  if (raw.length > 0 && isRunEventDict(raw[0])) {
    const view = runViewFromEvents(raw as unknown as RunEvent[], {
      task: snap.task,
    });
    return {
      ...view,
      status,
      output: snap.output ?? view.output,
      error: snap.error ?? view.error,
    };
  }

  const persisted = raw as unknown as PersistedStep[];
  const steps = persisted.map(stepToRunStep);
  return {
    task: snap.task,
    status,
    tier: steps.find((s) => s.tier)?.tier,
    recall: persistedRecall(persisted[0]),
    steps,
    output: snap.output ?? undefined,
    error: snap.error ?? undefined,
  };
}
