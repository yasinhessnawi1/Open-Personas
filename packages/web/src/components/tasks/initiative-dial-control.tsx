"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/auth";
import { useToast } from "@/components/patterns/toast";
import { Button } from "@/components/ui/button";
import {
  getInitiativeDial,
  type InitiativeDialState,
  setInitiativeDial,
} from "@/lib/api/tasks-client";
import { cn } from "@/lib/utils";

const LEVELS = ["off", "propose_only", "act_within_envelope"] as const;
type Level = (typeof LEVELS)[number];

/**
 * The persona's initiative restraint dial (Spec A6, B4) — in-context on the task detail (A6-D-1).
 *
 * Reflects the DURABLE level (fetched, never assumed). Setting it rides the shared write path; the
 * platform flag is surfaced honestly — when initiative is off, the note says the level won't act
 * until it's enabled (a true durable write, not a lie).
 */
export function InitiativeDialControl({ personaId }: { personaId: string }) {
  const t = useTranslations("taskDetail");
  const { getToken } = useAuth();
  const toast = useToast();
  const [state, setState] = useState<InitiativeDialState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setState(await getInitiativeDial(await getToken(), personaId));
      } catch {
        setState(null);
      }
    })();
  }, [getToken, personaId]);

  const choose = useCallback(
    async (level: Level) => {
      setBusy(true);
      try {
        setState(await setInitiativeDial(await getToken(), personaId, level));
      } catch {
        toast.error(t("dialFailed"));
      } finally {
        setBusy(false);
      }
    },
    [getToken, personaId, toast, t],
  );

  if (state === null) return null;

  return (
    <div className="flex flex-col gap-2" data-slot="initiative-dial">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {t("dialLabel")}
      </p>
      <div className="flex flex-wrap gap-2">
        {LEVELS.map((level) => (
          <Button
            key={level}
            size="sm"
            variant={state.dial === level ? "default" : "outline"}
            disabled={busy}
            aria-pressed={state.dial === level}
            onClick={() => choose(level)}
          >
            {t(`dial.${level}`)}
          </Button>
        ))}
      </div>
      {!state.initiative_enabled ? (
        <p
          className={cn("text-xs text-amber-600 dark:text-amber-500")}
          data-slot="dial-disabled-note"
        >
          {t("dialDisabled")}
        </p>
      ) : null}
    </div>
  );
}
