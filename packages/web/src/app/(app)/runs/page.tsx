import { redirect } from "next/navigation";

/**
 * Spec W1 (D-W1-14): the standalone runs index retired. A run is an execution of a task, so
 * the work list lives under Activity and each task's detail hosts its run history; the run
 * viewer at `/runs/{id}` stays as the step-level drill. Old links land on the work list.
 */
export default function RunsIndexPage(): never {
  redirect("/activity/tasks");
}
