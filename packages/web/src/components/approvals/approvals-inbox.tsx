"use client";

import { ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/auth";
import { Stack } from "@/components/layout";
import { EmptyState } from "@/components/patterns/empty-state";
import { SkeletonBlock } from "@/components/patterns/loading";
import { useToast } from "@/components/patterns/toast";
import {
  type ApprovalDecisionRequest,
  type ApprovalOut,
  decideApproval,
  fetchApprovals,
  getApproval,
} from "@/lib/api/approvals-client";
import { useTaskSignal } from "@/lib/task-signal";

import { ApprovalCard, type CardResolution } from "./approval-card";

/** persona_id → display name (from the server-fetched persona list; falls back to the id). */
export type PersonaNames = Record<string, string>;

export function ApprovalsInbox({
  personaNames,
}: {
  personaNames: PersonaNames;
}) {
  const t = useTranslations("approvals");
  const { getToken } = useAuth();
  const toast = useToast();
  const [approvals, setApprovals] = useState<ApprovalOut[] | null>(null);
  const [resolutions, setResolutions] = useState<
    Record<string, CardResolution>
  >({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      setApprovals(await fetchApprovals(await getToken()));
    } catch {
      setApprovals([]);
      toast.error(t("loadFailed"));
    }
  }, [getToken, toast, t]);

  useEffect(() => {
    void load();
  }, [load]);

  // W8: a task parking on the user (an approval) refetches the durable pending list (A6-R-4).
  useTaskSignal(() => void load());

  const onDecide = useCallback(
    async (proposalId: string, req: ApprovalDecisionRequest) => {
      setBusy((b) => ({ ...b, [proposalId]: true }));
      try {
        const result = await decideApproval(await getToken(), proposalId, req);
        // A material modify stays pending + revises the payload — refetch to show the new args.
        if (result.outcome === "modify" && result.note === "reconfirm") {
          const revised = await getApproval(await getToken(), proposalId);
          setApprovals((list) =>
            (list ?? []).map((a) =>
              a.proposal_id === proposalId ? revised : a,
            ),
          );
          setResolutions((r) => ({
            ...r,
            [proposalId]: {
              outcome: "modify",
              status: result.status,
              note: result.note,
            },
          }));
        } else {
          // approve / deny / already-handled — reflect the DURABLE outcome (A6-D-3), never error.
          setResolutions((r) => ({
            ...r,
            [proposalId]: {
              outcome: result.outcome,
              status: result.status,
              note: result.note,
            },
          }));
        }
      } catch {
        toast.error(t("decideFailed"));
      } finally {
        setBusy((b) => ({ ...b, [proposalId]: false }));
      }
    },
    [getToken, toast, t],
  );

  if (approvals === null) {
    return (
      <Stack gap={3}>
        <SkeletonBlock className="h-24" />
        <SkeletonBlock className="h-24" />
      </Stack>
    );
  }

  if (approvals.length === 0) {
    return (
      <EmptyState
        icon={<ShieldCheck className="size-6" />}
        title={t("emptyTitle")}
        description={t("emptyBody")}
      />
    );
  }

  return (
    <Stack gap={3}>
      {approvals.map((approval) => (
        <ApprovalCard
          key={approval.proposal_id}
          approval={approval}
          personaName={personaNames[approval.persona_id] ?? approval.persona_id}
          resolution={resolutions[approval.proposal_id] ?? null}
          busy={busy[approval.proposal_id] ?? false}
          onDecide={(req) => onDecide(approval.proposal_id, req)}
        />
      ))}
    </Stack>
  );
}
