"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import { Card } from "@/components/ui/card";
import { unwrap } from "@/lib/api/client";
import { useApi } from "@/lib/api/use-api";

/**
 * Spec M5 (T4a) — the auto-top-up toggle (B1).
 *
 * M4 shipped the auto-top-up engine and the column it reads, but nothing could set it
 * (§1c.5). This is the switch, and it is deliberately the only thing the user controls:
 * the threshold and the amount are owner-locked constants (D-M4-R5) read from the API,
 * never hardcoded here and never posted from the browser. A client that could name its
 * own top-up amount would be naming its own charge.
 *
 * Eligibility is the SERVER's answer, not a guess: an ineligible plan gets a 409, which
 * this surfaces as honest copy rather than pre-hiding the control on a locally-inferred
 * rule that could drift from the engine's (D-M5-28).
 *
 * The switch reflects the STORED state the server returns, so a refused change snaps
 * back instead of leaving the UI claiming something the engine will not honour.
 */
export interface AutoTopupCardProps {
  /** The caller's plan, from the wallet — decides whether the control is offered. */
  readonly planCode: string;
  /** Whether the plan offers auto-top-up at all, from the CATALOG (never inferred). */
  readonly eligible: boolean;
  /** The stored state at page load, from the wallet. */
  readonly initialEnabled: boolean;
  /** Owner-locked constants from the catalog, for the copy. */
  readonly thresholdCredits: number;
  readonly amountCredits: number;
}

function dollars(credits: number): string {
  return (credits / 100).toFixed(credits % 100 === 0 ? 0 : 2);
}

export function AutoTopupCard({
  planCode,
  eligible,
  initialEnabled,
  thresholdCredits,
  amountCredits,
}: AutoTopupCardProps) {
  const t = useTranslations("billing");
  const api = useApi();
  const [enabled, setEnabled] = useState(initialEnabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setEnabled(initialEnabled), [initialEnabled]);

  const toggle = useCallback(
    async (next: boolean) => {
      setBusy(true);
      setError(null);
      try {
        const result = await unwrap(
          await api.PATCH("/v1/me/billing/auto-topup", {
            body: { enabled: next },
          }),
        );
        // Trust the STORED value, not the requested one: if the server refused or
        // clamped, the switch must show what is actually armed.
        setEnabled(result.enabled);
      } catch {
        setError(t("autoTopupFailed"));
        setEnabled(initialEnabled);
      } finally {
        setBusy(false);
      }
    },
    [api, t, initialEnabled],
  );

  // Not offered on this plan: say so plainly instead of rendering a switch that 409s.
  if (!eligible) {
    return (
      <Card
        className="p-6"
        data-slot="auto-topup-unavailable"
        data-plan={planCode}
      >
        <h2 className="type-caption font-mono uppercase text-muted-foreground">
          {t("autoTopupLabel")}
        </h2>
        <p className="type-body mt-2 max-w-prose text-muted-foreground">
          {t("autoTopupProOnly")}
        </p>
      </Card>
    );
  }

  return (
    <Card className="p-6" data-slot="auto-topup" data-enabled={String(enabled)}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="type-caption font-mono uppercase text-muted-foreground">
            {t("autoTopupLabel")}
          </h2>
          <p className="type-body mt-2 max-w-prose text-muted-foreground">
            {t("autoTopupExplain", {
              threshold: dollars(thresholdCredits),
              amount: dollars(amountCredits),
            })}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={t("autoTopupLabel")}
          disabled={busy}
          onClick={() => toggle(!enabled)}
          className="v-toggle"
          data-on={enabled ? "true" : "false"}
          data-slot="auto-topup-switch"
        />
      </div>
      {error !== null ? (
        <p className="type-caption mt-3" data-slot="auto-topup-error">
          {error}
        </p>
      ) : null}
    </Card>
  );
}
