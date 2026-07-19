import type {
  LedgerTotals,
  SurfaceGroup,
  SurfaceKey,
} from "@/lib/usage-surfaces";
import { creditShare } from "@/lib/usage-surfaces";

/**
 * Spec M3 — all-surface usage breakdown.
 *
 * Renders the `GET /v1/me/usage/ledger` ledger folded by surface: one row per
 * surface with the credits charged, a proportional spend bar (its share of the
 * period's total — the card's one signature element), the event count, and the
 * cost provenance + summed provider cost. A bold total closes the list.
 *
 * Pure presentational, following the `<LowBalanceWarningCard>` precedent:
 * strings + number formatting are pre-resolved by the (server) settings page,
 * so this stays client-safe and unit-testable without next-intl.
 */
export interface UsageBreakdownCopy {
  /** Localised surface label, e.g. `chat → "Chat"`. */
  readonly surface: (key: SurfaceKey) => string;
  /** Localised provenance label, e.g. `actual_openrouter → "Metered"`. `null` → em dash. */
  readonly basis: (basis: string | null) => string;
  /** Localised, pluralised event count, e.g. `3 → "3 events"`. */
  readonly events: (count: number) => string;
  /** Format a credits amount (locale-pinned integer). */
  readonly credits: (amount: number) => string;
  /** Format a provider cost, given cents. */
  readonly cost: (cents: number) => string;
  readonly creditsLabel: string;
  readonly costLabel: string;
  readonly totalLabel: string;
}

export interface UsageBreakdownProps {
  readonly groups: readonly SurfaceGroup[];
  readonly total: LedgerTotals;
  readonly copy: UsageBreakdownCopy;
}

export function UsageBreakdown({ groups, total, copy }: UsageBreakdownProps) {
  return (
    <div data-slot="settings-usage-breakdown">
      <div className="type-caption flex items-center justify-between border-b pb-2 text-muted-foreground">
        <span>{copy.creditsLabel}</span>
        <span>{copy.costLabel}</span>
      </div>
      <ul className="flex flex-col">
        {groups.map((group) => (
          <li
            key={group.key}
            className="border-b py-3 last:border-0"
            data-slot="settings-usage-surface"
            data-surface={group.key}
          >
            <div className="flex items-baseline justify-between gap-3">
              <span className="type-body font-medium">
                {copy.surface(group.key)}
              </span>
              <span
                className="type-body tabular-nums"
                data-slot="surface-credits"
              >
                {copy.credits(group.creditsCharged)}
              </span>
            </div>
            <div
              className="mt-2 h-1 overflow-hidden rounded-full bg-muted"
              aria-hidden
            >
              <div
                className="h-full rounded-full bg-primary"
                style={{
                  width: `${Math.round(creditShare(group, total) * 100)}%`,
                }}
              />
            </div>
            <div className="type-caption mt-1.5 flex items-center justify-between gap-3 text-muted-foreground">
              <span>
                {copy.events(group.count)}
                {group.basis ? ` · ${copy.basis(group.basis)}` : null}
              </span>
              <span className="tabular-nums" data-slot="surface-cost">
                {group.hasCost ? copy.cost(group.costCents) : copy.basis(null)}
              </span>
            </div>
          </li>
        ))}
      </ul>
      <div
        className="mt-1 flex items-baseline justify-between gap-3 border-t pt-3"
        data-slot="settings-usage-total"
      >
        <span className="type-body font-medium">{copy.totalLabel}</span>
        <span className="type-body font-medium tabular-nums">
          {copy.credits(total.creditsCharged)}
          {total.hasCost ? (
            <span className="type-caption ml-2 font-normal text-muted-foreground">
              {copy.cost(total.costCents)}
            </span>
          ) : null}
        </span>
      </div>
    </div>
  );
}
