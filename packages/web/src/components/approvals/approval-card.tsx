"use client";

import { ChevronDown, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type {
  ApprovalDecisionRequest,
  ApprovalOut,
  JsonValue,
} from "@/lib/api/approvals-client";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { cn } from "@/lib/utils";

/** A resolution outcome to reflect on the card (A6-D-3 — read from the durable record). */
export interface CardResolution {
  outcome: string | null;
  status: string;
  note: string;
}

/** The statuses that mean the proposal is decided and the card should rest. */
const TERMINAL_STATUSES = new Set([
  "approved",
  "modified",
  "denied",
  "expired",
  "consumed",
]);

/**
 * Which reflection sentence this card shows, from the durable record first.
 *
 * One function for both halves of the surface: a card answered a moment ago (it has a live
 * `resolution`) and a card read back from `/v1/approvals/handled` on a fresh page (it has only
 * its own row). Both end up at the same sentence, which is the point. Before this, "what
 * happened to that approval" was something only the tab that did it could tell you.
 *
 * The edit is the part worth getting right. `modified` is the status a decided-with-edits
 * proposal holds until the action runs; after that everything is `consumed`, so `edited` (the
 * durable decision trail) is what still remembers whose version of the action went out.
 *
 * Returns null when there is nothing to say yet (still pending, nothing decided).
 */
export function reflectionKey(
  approval: Pick<ApprovalOut, "status" | "edited">,
  resolution: CardResolution | null,
): string | null {
  // The race loser: the durable record is someone else's decision, so say so and stop.
  if (resolution !== null && resolution.outcome === null)
    return "alreadyHandled";
  const status = resolution?.status ?? approval.status;
  const edited =
    approval.edited ||
    status === "modified" ||
    resolution?.outcome === "modify";
  if (status === "modified") return "approvedWithEdits";
  if (status === "approved" || status === "consumed")
    return edited ? "approvedWithEdits" : "approved";
  if (status === "denied") return "denied";
  if (status === "expired") return "expiredReflection";
  // Still pending. Only a live answer can say anything, and the only one that leaves a
  // proposal pending is the material edit that has gone back for a second confirmation.
  if (resolution === null) return null;
  if (resolution.outcome === "modify") return "reconfirm";
  return "resolvedGeneric";
}

interface ApprovalCardProps {
  approval: ApprovalOut;
  personaName: string;
  resolution: CardResolution | null;
  busy: boolean;
  onDecide: (req: ApprovalDecisionRequest) => void;
}

/** Render a value verbatim as TEXT — never HTML (a safety surface; React escapes strings). */
function renderValue(
  value: JsonValue,
  t: ReturnType<typeof useTranslations>,
): string {
  if (value === null) return t("valueNone");
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function expiryLabel(
  iso: string,
  t: ReturnType<typeof useTranslations>,
): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return "";
  if (ms <= 0) return t("expired");
  const hours = Math.round(ms / 3_600_000);
  if (hours < 24) return t("expiresInHours", { hours: Math.max(1, hours) });
  return t("expiresInDays", { days: Math.round(hours / 24) });
}

/** The calm A6-D-3 reflection: what the durable record says happened, never an error. */
function ResolutionBanner({
  reflection,
  t,
}: {
  reflection: string;
  t: ReturnType<typeof useTranslations>;
}) {
  const key = reflection;
  const good = key === "approved" || key === "approvedWithEdits";
  return (
    <output
      className={cn(
        "block rounded-md px-3 py-2 text-sm",
        good
          ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400"
          : "bg-muted text-muted-foreground",
      )}
    >
      {t(`reflection.${key}`)}
    </output>
  );
}

export function ApprovalCard({
  approval,
  personaName,
  resolution,
  busy,
  onDecide,
}: ApprovalCardProps) {
  const t = useTranslations("approvals");
  const [expanded, setExpanded] = useState(false);
  const [modifying, setModifying] = useState(false);
  const [edited, setEdited] = useState<Record<string, string>>({});

  const reflection = reflectionKey(approval, resolution);
  // The card rests once the record is decided: either the row itself is terminal (a handled
  // card read back on a fresh page) or the live answer settled it. Only the material edit's
  // re-confirm leaves it open, which is exactly the case that still needs the buttons.
  const resolved =
    TERMINAL_STATUSES.has(approval.status) ||
    (resolution !== null &&
      (resolution.outcome === null ||
        resolution.outcome === "approve" ||
        resolution.outcome === "deny" ||
        TERMINAL_STATUSES.has(resolution.status)));

  const args = Object.entries(approval.arguments);

  function startModify() {
    // Seed the editor with the current top-level values as text (the common email/amount case).
    const seed: Record<string, string> = {};
    for (const [k, v] of args)
      seed[k] = typeof v === "string" ? v : JSON.stringify(v);
    setEdited(seed);
    setModifying(true);
  }

  function submitModify() {
    // Send the edited payload; the resolver's floor decides materiality (material → re-confirm).
    const editedArgs: Record<string, JsonValue> = {};
    for (const [k, v] of Object.entries(edited)) {
      const original = approval.arguments[k];
      // keep non-string originals as-is unless the text actually changed (avoid stringifying back)
      editedArgs[k] =
        typeof original === "string" || original === undefined
          ? v
          : (original as JsonValue);
      if (typeof original !== "string" && v !== JSON.stringify(original)) {
        try {
          editedArgs[k] = JSON.parse(v) as JsonValue;
        } catch {
          editedArgs[k] = v;
        }
      }
    }
    onDecide({ decision: "modify", edited_arguments: editedArgs });
    setModifying(false);
  }

  return (
    <Card
      style={personaIdentityStyle({ id: approval.persona_id })}
      className={cn(
        "overflow-hidden border-l-2",
        resolved ? "border-l-border opacity-70" : "border-l-amber-500",
      )}
      data-slot="approval-card"
    >
      <CardContent className="flex flex-col gap-3 p-4">
        {/* header — persona + state + expiry (always visible) */}
        <div className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="size-2.5 rounded-[3px]"
            style={{
              background: "var(--v-id)",
              boxShadow:
                "0 0 0 3px oklch(var(--identity-l) calc(var(--identity-c) * 0.25) var(--identity-h))",
            }}
          />
          <span className="text-sm font-medium">{personaName}</span>
          {/* A decided card is not waiting on anyone, so it carries neither the amber
              "needs approval" badge nor a countdown to a deadline that no longer applies. */}
          {resolved ? null : (
            <>
              <Badge
                variant="outline"
                className="text-amber-600 dark:text-amber-500"
              >
                {t("needsApproval")}
              </Badge>
              <span className="ml-auto text-xs tabular-nums text-muted-foreground">
                {expiryLabel(approval.expires_at, t)}
              </span>
            </>
          )}
        </div>

        <p className="type-body">{approval.description}</p>

        {reflection ? <ResolutionBanner reflection={reflection} t={t} /> : null}

        {/* see-then-grant: the proposal + actions are gated behind expand — you cannot approve
            without first seeing the exact recorded payload (the informed-consent grammar). */}
        {!resolved ? (
          <>
            <Button
              variant="ghost"
              size="sm"
              className="-ml-2 w-fit"
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
              data-icon="inline-start"
            >
              <ChevronDown
                className={cn("transition-transform", expanded && "rotate-180")}
              />
              {expanded ? t("hideDetails") : t("review")}
            </Button>

            {expanded ? (
              <div className="flex flex-col gap-3">
                {/* the EXACT recorded payload — verbatim, as text (never HTML) */}
                <div className="rounded-md border border-border-soft bg-muted/40 p-3">
                  <p className="mb-2 flex items-center gap-1.5 text-xs uppercase tracking-wide text-muted-foreground">
                    <ShieldCheck className="size-3.5" />
                    {t("whatWillHappen", { tool: approval.tool_name })}
                  </p>
                  <dl className="flex flex-col gap-2">
                    {args.map(([key, value]) => (
                      <div
                        key={key}
                        className="grid grid-cols-[minmax(4rem,auto)_1fr] gap-2"
                      >
                        <dt className="text-xs font-medium text-muted-foreground">
                          {key}
                        </dt>
                        {modifying && typeof value === "string" ? (
                          <textarea
                            className="min-h-8 w-full resize-y rounded border border-border bg-card px-2 py-1 text-sm"
                            value={edited[key] ?? ""}
                            onChange={(e) =>
                              setEdited((s) => ({
                                ...s,
                                [key]: e.target.value,
                              }))
                            }
                          />
                        ) : (
                          <dd className="whitespace-pre-wrap break-words text-sm">
                            {renderValue(value, t)}
                          </dd>
                        )}
                      </div>
                    ))}
                  </dl>
                </div>

                {/* the grant — below the full proposal, seen-then-granted */}
                <div className="flex flex-wrap gap-2">
                  {modifying ? (
                    <>
                      <Button disabled={busy} onClick={submitModify}>
                        {t("submitChange")}
                      </Button>
                      <Button
                        variant="ghost"
                        disabled={busy}
                        onClick={() => setModifying(false)}
                      >
                        {t("cancel")}
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button
                        disabled={busy}
                        onClick={() => onDecide({ decision: "approve" })}
                      >
                        {t("approve")}
                      </Button>
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={startModify}
                      >
                        {t("modify")}
                      </Button>
                      <Button
                        variant="ghost"
                        disabled={busy}
                        onClick={() => onDecide({ decision: "deny" })}
                      >
                        {t("deny")}
                      </Button>
                    </>
                  )}
                </div>
              </div>
            ) : null}
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}
