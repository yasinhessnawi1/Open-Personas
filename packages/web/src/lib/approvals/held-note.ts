/**
 * Why an answered approval's task waits (D-ORCH-11-18-d (2); PR 34 review, finding C).
 *
 * The resolver carries out a decision on a paused task, or on one at its budget cap, at once
 * and holds only the next leg. It says so with a machine note, like its other notes
 * (`reconfirm`, `race_lost`): `held_paused`, `held_at_cap`, or `hold_failed` when the action
 * ran but the task could not be set to carry on. The inbox card and the chat turn both turn
 * that note into the same sentence, from `approvals.held` in the message catalogue.
 */
export type HeldNoteKey = "heldWhilePaused" | "heldAtTheCap" | "holdFailed";

const HELD_NOTES: Readonly<Record<string, HeldNoteKey>> = {
  held_paused: "heldWhilePaused",
  held_at_cap: "heldAtTheCap",
  hold_failed: "holdFailed",
};

/** The catalogue key for a resolver note, or null when the task was not held. */
export function heldNoteKey(
  note: string | null | undefined,
): HeldNoteKey | null {
  if (!note) return null;
  return Object.hasOwn(HELD_NOTES, note) ? HELD_NOTES[note] : null;
}
