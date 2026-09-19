"use server";

import { redirect } from "next/navigation";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/** One attached file as the hand-off dialog posts it (issue #16). */
interface AttachmentField {
  ref: string;
  filename: string;
  media_type: string;
}

/**
 * Read the dialog's hidden `attachments` field back into typed refs.
 *
 * The field is a JSON array of workspace refs the uploads endpoint already returned, so a
 * malformed or truncated value means the form is not one we wrote: drop it and dispatch
 * the task anyway rather than losing the goal the person typed.
 */
function readAttachments(raw: FormDataEntryValue | null): AttachmentField[] {
  if (typeof raw !== "string" || raw.trim() === "") return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((entry) => {
      if (typeof entry !== "object" || entry === null) return [];
      const { ref, filename, media_type } = entry as Record<string, unknown>;
      if (typeof ref !== "string" || ref === "") return [];
      return [
        {
          ref,
          filename: typeof filename === "string" ? filename : "",
          media_type: typeof media_type === "string" ? media_type : "",
        },
      ];
    });
  } catch {
    return [];
  }
}

/**
 * Spec 35 — Tasks-page dispatch. Unlike the per-persona `startRun` (bound to a
 * single persona), this reads BOTH the chosen persona and the task from the
 * form, dispatches the agentic run, and jumps to the run viewer.
 *
 * Issue #16: it also carries the files the person attached to the description, so the
 * persona opens them on its first leg instead of working from the sentence alone.
 */
export async function startTask(formData: FormData) {
  const personaId = String(formData.get("persona_id") ?? "").trim();
  const task = String(formData.get("task") ?? "").trim();
  if (!personaId || !task) return; // the form disables submit on empty input.
  const attachments = readAttachments(formData.get("attachments"));
  const api = await serverApi();
  const dispatched = await unwrap(
    await api.POST("/v1/personas/{persona_id}/runs", {
      params: { path: { persona_id: personaId } },
      body: { task, attachments },
    }),
  );
  // Spec W1 (D-W1-3): a one-off is an ad hoc task; its detail hosts the run as the worker
  // opens it, so the dialog lands there rather than on a run that does not exist yet.
  redirect(`/activity/tasks/${dispatched.task_id}`);
}
