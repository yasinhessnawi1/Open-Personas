"use client";

import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/auth";
import { useToast } from "@/components/patterns/toast";
import { InitiativeDialControl } from "@/components/tasks/initiative-dial-control";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  type AutonomyState,
  getAutonomyState,
  getPersonaSuspension,
  type PersonaSuspension,
  pauseAutonomy,
  resumeAutonomy,
  resumePersona,
  suspendPersona,
} from "@/lib/api/autonomy-client";
import { personaIdentityStyle } from "@/lib/persona-identity";

/**
 * The autonomy kill switches (Spec A6, W7) — the owner-wide pause at the area root + per-persona
 * suspend/dial, rendering B4's three switches.
 *
 * HONESTY CONSTRAINT: the copy says only what is true NOW. Until the owner-pause predicate is
 * injected into A7/A10/A5 origination at merge-back, these switches stop the task-leg path but not
 * yet those origination sources — so the copy is "pause new autonomous actions" (task-leg scoped),
 * never "stops all autonomy". The copy strengthens at merge-back once completeness is wired.
 */
export function AutonomyControls({
  personas,
}: {
  personas: { id: string; name: string }[];
}) {
  const t = useTranslations("autonomyControls");
  // The per-persona rows each fetch their suspension + dial on mount; keep them behind a disclosure
  // so the Review landing stays a light, calm glance and only fetches when the user manages.
  const [showPersonas, setShowPersonas] = useState(false);
  return (
    <section className="flex flex-col gap-4" data-slot="autonomy-controls">
      <div className="flex flex-col gap-1">
        <h2 className="type-heading">{t("title")}</h2>
        <p className="type-caption text-muted-foreground">{t("subtitle")}</p>
      </div>
      <OwnerPauseSwitch />
      {personas.length > 0 ? (
        <div className="flex flex-col gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="-ml-2 w-fit"
            data-icon="inline-start"
            aria-expanded={showPersonas}
            onClick={() => setShowPersonas((v) => !v)}
          >
            <ChevronDown
              className={
                showPersonas
                  ? "rotate-180 transition-transform"
                  : "transition-transform"
              }
            />
            {t("byPersona")}
          </Button>
          {showPersonas
            ? personas.map((p) => (
                <PersonaRow key={p.id} personaId={p.id} personaName={p.name} />
              ))
            : null}
        </div>
      ) : null}
    </section>
  );
}

function OwnerPauseSwitch() {
  const t = useTranslations("autonomyControls");
  const { getToken } = useAuth();
  const toast = useToast();
  const [state, setState] = useState<AutonomyState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setState(await getAutonomyState(await getToken()));
      } catch {
        setState(null);
      }
    })();
  }, [getToken]);

  const toggle = useCallback(async () => {
    if (!state) return;
    setBusy(true);
    try {
      const token = await getToken();
      // reflect the DURABLE post-state (calm, idempotent) — never assume the flip.
      setState(
        state.paused ? await resumeAutonomy(token) : await pauseAutonomy(token),
      );
    } catch {
      toast.error(t("failed"));
    } finally {
      setBusy(false);
    }
  }, [state, getToken, toast, t]);

  if (state === null) return null;

  return (
    <Card data-slot="owner-pause" data-paused={state.paused}>
      <CardContent className="flex flex-wrap items-center gap-3 p-4">
        <div className="flex min-w-0 flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">{t("ownerLabel")}</span>
            <Badge
              variant="outline"
              className={
                state.paused ? "text-amber-600 dark:text-amber-500" : undefined
              }
            >
              {state.paused ? t("statusPaused") : t("statusActive")}
            </Badge>
          </div>
          {/* honest scope — what a pause actually stops right now (not "all autonomy") */}
          <p className="type-caption text-muted-foreground">{t("ownerHint")}</p>
        </div>
        <Button
          className="ml-auto"
          size="sm"
          variant={state.paused ? "default" : "outline"}
          disabled={busy}
          onClick={toggle}
        >
          {state.paused ? t("resume") : t("pause")}
        </Button>
      </CardContent>
    </Card>
  );
}

function PersonaRow({
  personaId,
  personaName,
}: {
  personaId: string;
  personaName: string;
}) {
  const t = useTranslations("autonomyControls");
  const { getToken } = useAuth();
  const toast = useToast();
  const [state, setState] = useState<PersonaSuspension | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setState(await getPersonaSuspension(await getToken(), personaId));
      } catch {
        setState(null);
      }
    })();
  }, [getToken, personaId]);

  const toggle = useCallback(async () => {
    if (!state) return;
    setBusy(true);
    try {
      const token = await getToken();
      setState(
        state.suspended
          ? await resumePersona(token, personaId)
          : await suspendPersona(token, personaId),
      );
    } catch {
      toast.error(t("failed"));
    } finally {
      setBusy(false);
    }
  }, [state, getToken, personaId, toast, t]);

  return (
    <Card style={personaIdentityStyle({ id: personaId })}>
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span
            aria-hidden="true"
            className="size-2.5 rounded-[3px]"
            style={{ background: "var(--v-id)" }}
          />
          <span className="text-sm font-medium">{personaName}</span>
          {state?.suspended ? (
            <Badge
              variant="outline"
              className="text-amber-600 dark:text-amber-500"
            >
              {t("suspended")}
            </Badge>
          ) : null}
          {state !== null ? (
            <Button
              className="ml-auto"
              size="sm"
              variant={state.suspended ? "default" : "outline"}
              disabled={busy}
              onClick={toggle}
            >
              {state.suspended ? t("resumePersona") : t("suspend")}
            </Button>
          ) : null}
        </div>
        <InitiativeDialControl personaId={personaId} />
      </CardContent>
    </Card>
  );
}
