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
  resolution,
  t,
}: {
  resolution: CardResolution;
  t: ReturnType<typeof useTranslations>;
}) {
  const { outcome } = resolution;
  let key = "resolvedGeneric";
  let good = false;
  if (outcome === null)
    key = "alreadyHandled"; // the race loser — reflect, never error
  else if (outcome === "approve") {
    key = "approved";
    good = true;
  } else if (outcome === "deny") key = "denied";
  else if (outcome === "modify") key = "reconfirm";
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

  // A terminal reflection (approved / denied / already handled) — the card rests, resolved.
  const resolved =
    resolution !== null &&
    (resolution.outcome === null ||
      resolution.outcome === "approve" ||
      resolution.outcome === "deny");

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
        "overflow-hidden border-l-2 border-l-amber-500",
        resolved && "opacity-70",
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
          <Badge
            variant="outline"
            className="text-amber-600 dark:text-amber-500"
          >
            {t("needsApproval")}
          </Badge>
          <span className="ml-auto text-xs tabular-nums text-muted-foreground">
            {expiryLabel(approval.expires_at, t)}
          </span>
        </div>

        <p className="type-body">{approval.description}</p>

        {resolution ? <ResolutionBanner resolution={resolution} t={t} /> : null}

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
