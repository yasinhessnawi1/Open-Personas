"use client";

/**
 * Spec W1 (T7) — the verbs on a review line, acting where you are reading.
 *
 * Q2's whole point: seeing that something needs you and being unable to do anything about it
 * from there is not a review, it is a list of regrets. Every waiting line already knows what
 * can be done to it (the server decides `actions` from what the item IS, D-W1-6), so the
 * surface renders those verbs and calls the matching task command. It never invents a verb,
 * and it never hides one: a line with no verbs falls back to its deep link.
 *
 * Two rules the whole file follows:
 *
 * - **Refetch, never trust (A6-R-4).** A command's reply is not folded into the rendered list.
 *   The digest is re-read afterwards, so what you see is the durable truth, and the badge
 *   (which is that same list's length, D-W1-5) moves with it.
 * - **A no-op is calm, never an error (B2).** The commands are idempotent; a second press, a
 *   task that finished a moment ago, or a paused autonomy dial answers `changed: false` with
 *   an honest sentence, and that sentence is what the user is shown.
 *
 * Approvals are deliberately NOT actioned from here. Their verbs (`approve` / `decline`) act
 * on a proposal whose arguments live one screen away, and the see-then-grant grammar this
 * product holds everywhere else says you look at what you are granting before you grant it.
 * The review line keeps its link into the inbox; {@link isActionable} is what draws that line.
 */

import { useState } from "react";

import { useAuth } from "@/auth";
import { useToast } from "@/components/patterns/toast";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { DigestItem } from "@/lib/api/review-client";
import {
  cancelTask,
  pickupTask,
  replyToTask,
  retryTask,
  type TaskCommandResult,
} from "@/lib/api/tasks-client";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";

/** The verbs this surface can carry out itself (the task commands). */
const TASK_VERBS = ["reply", "pickup", "cancel", "retry"] as const;
export type TaskVerb = (typeof TASK_VERBS)[number];

/** The cap the reply route enforces (D-W1-7); the box stops you before the server has to. */
export const MAX_REPLY_CHARS = 8000;

function isTaskVerb(action: string): action is TaskVerb {
  return (TASK_VERBS as readonly string[]).includes(action);
}

/**
 * The task verbs this line offers, in a fixed order so the primary action never moves under
 * the cursor between two lines that offer different sets. Empty for an approval line (its
 * verbs belong to the inbox, where the proposal is visible) or a line with no task behind it.
 */
export function actionableVerbs(item: DigestItem): TaskVerb[] {
  if (item.ref?.kind !== "task") return [];
  const offered = new Set(item.actions.filter(isTaskVerb));
  return TASK_VERBS.filter((verb) => offered.has(verb));
}

/** Whether this line can be acted on in place (vs. followed to its own surface). */
export function isActionable(item: DigestItem): boolean {
  return actionableVerbs(item).length > 0;
}

export function AttentionActions({
  item,
  onActed,
  t,
}: {
  item: DigestItem;
  /** Re-read the digest: the list and the badge both move from the durable truth. */
  onActed: () => void;
  t: (key: string) => string;
}) {
  const { getToken } = useAuth();
  const toast = useToast();
  const refreshSidebar = useSidebarRefresh();
  const [busy, setBusy] = useState<TaskVerb | null>(null);
  const [composing, setComposing] = useState(false);
  const [reply, setReply] = useState("");

  const verbs = actionableVerbs(item);
  const taskId = item.ref?.kind === "task" ? item.ref.id : null;
  if (!taskId || verbs.length === 0) return null;

  const run = async (verb: TaskVerb, act: () => Promise<TaskCommandResult>) => {
    setBusy(verb);
    try {
      const result = await act();
      // The server's own sentence when nothing moved (already finished, autonomy paused, a
      // reply that belongs on an approval): honest beats a silent no-op.
      if (!result.changed) toast.info(result.note || t("actions.noChange"));
      else toast.success(t(`actions.done.${verb}`));
      setComposing(false);
      setReply("");
      onActed();
      refreshSidebar();
    } catch {
      toast.error(t("actions.failed"));
    } finally {
      setBusy(null);
    }
  };

  const press = (verb: TaskVerb) => {
    if (verb === "reply") {
      setComposing((open) => !open);
      return;
    }
    void run(verb, async () => {
      const token = await getToken();
      if (verb === "pickup") return pickupTask(token, taskId);
      if (verb === "retry") return retryTask(token, taskId);
      return cancelTask(token, taskId);
    });
  };

  const send = () => {
    const text = reply.trim();
    if (!text) return;
    void run("reply", async () => replyToTask(await getToken(), taskId, text));
  };

  return (
    <div className="flex flex-col gap-2" data-slot="attention-actions">
      <div className="flex flex-wrap gap-2">
        {verbs.map((verb, i) => (
          <Button
            key={verb}
            type="button"
            size="sm"
            // The first verb the server offers is the one it expects: a question wants an
            // answer, a stalled task wants picking up, a failed one wants another go.
            variant={
              i === 0 ? "default" : verb === "cancel" ? "ghost" : "outline"
            }
            disabled={busy !== null}
            aria-expanded={verb === "reply" ? composing : undefined}
            data-verb={verb}
            onClick={() => press(verb)}
          >
            {t(`actions.${verb}`)}
          </Button>
        ))}
      </div>

      {composing ? (
        <div className="flex flex-col gap-2">
          <Textarea
            aria-label={t("actions.replyLabel")}
            placeholder={t("actions.replyPlaceholder")}
            maxLength={MAX_REPLY_CHARS}
            rows={3}
            value={reply}
            data-slot="attention-reply"
            onChange={(e) => setReply(e.target.value)}
          />
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              disabled={busy !== null || reply.trim().length === 0}
              data-verb="reply-send"
              onClick={send}
            >
              {t("actions.send")}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy !== null}
              onClick={() => setComposing(false)}
            >
              {t("actions.dismiss")}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
