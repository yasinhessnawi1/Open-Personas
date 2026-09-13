"use server";

import { redirect } from "next/navigation";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec 35 — Tasks-page dispatch. Unlike the per-persona `startRun` (bound to a
 * single persona), this reads BOTH the chosen persona and the task from the
 * form, dispatches the agentic run, and jumps to the run viewer.
 */
export async function startTask(formData: FormData) {
  const personaId = String(formData.get("persona_id") ?? "").trim();
  const task = String(formData.get("task") ?? "").trim();
  if (!personaId || !task) return; // the form disables submit on empty input.
  const api = await serverApi();
  const dispatched = await unwrap(
    await api.POST("/v1/personas/{persona_id}/runs", {
      params: { path: { persona_id: personaId } },
      body: { task },
    }),
  );
  // Spec W1 (D-W1-3): a one-off is an ad hoc task; its detail hosts the run as the worker
  // opens it, so the dialog lands there rather than on a run that does not exist yet.
  redirect(`/activity/tasks/${dispatched.task_id}`);
}
