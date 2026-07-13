"use client";

import {
  Activity,
  CalendarClock,
  Phone,
  Sparkles,
  Waypoints,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

// Primary nav links. Settings is NOT here — it lives in the account footer
// menu (Spec 35 D-35-16). Each item may carry a live count (Spec 35 D-35-13).
const ITEMS = [
  // R11-B1 (D-R11-1/2): Home is retired; Activity is the FEATURED first row and
  // the app's landing surface (`/` redirects signed-in users to /activity).
  // Spec A6 (W5, A6-D-1): the Activity area — ONE nav row landing on the morning Review; Tasks +
  // Approvals are siblings WITHIN the area (the ActivityTabs sub-nav), not top-level rows.
  // R9-010: badge = the active working set (non-terminal tasks), never history.
  { href: "/activity", key: "activity", icon: Activity, count: "activity" },
  { href: "/personas", key: "personas", icon: Sparkles, count: "personas" },
  // R9-009: the Conversations row is FOLDED into the MESSAGES section — its
  // "All chats (N)" affordance links to /conversations (the route + page stay;
  // only this nav row went). The command palette's "Go to conversations" entry
  // deliberately remains — that's search, not nav.
  // Spec V9: the voice-call history surface. R9-010: badge = total call records.
  { href: "/calls", key: "calls", icon: Phone, count: "calls" },
  // Spec K5: the interactive knowledge-graph — "what your personas know, yours to shape."
  // R9-010: badge = canonical graph node count.
  { href: "/memory", key: "memory", icon: Waypoints, count: "memory" },
  // Spec A8: the schedule/calendar surface (time's view of the personas'
  // commitments). The route + calendar shipped styled but was unreachable —
  // reachable only by typed URL — until this nav row (R4-C1-10, built-but-inert
  // at the nav level). A6 may later re-home it alongside the review inbox.
  // R9-010: badge = schedule ROWS (a recurring schedule counts once, never fires).
  {
    href: "/schedule",
    key: "schedule",
    icon: CalendarClock,
    count: "schedule",
  },
  // Connectors deliberately has NO row here (R11-B1 amend, owner-ruled): it's a
  // one-time setup surface, so it stays a settings subsection reached from the
  // account menu + ⌘K — a permanent tab would outlive its usefulness.
] as const;

/**
 * Live counts shown on nav rows (Spec 35 D-35-13; R9-010 extends the set to
 * every row) — resolved from `GET /v1/me/nav-counts` via the sidebar data.
 * A zero/undefined count renders NO badge (zero-hidden).
 */
export interface NavCounts {
  readonly personas?: number;
  readonly calls?: number;
  /** Non-terminal (in-progress / waiting) tasks — the active working set. */
  readonly activity?: number;
  /** Canonical knowledge-graph nodes. */
  readonly memory?: number;
  /** Schedule rows — a recurring schedule counts once, never its fires. */
  readonly schedule?: number;
}

export function Nav({
  onNavigate,
  collapsed = false,
  counts,
  memoryAvailable = false,
}: {
  onNavigate?: () => void;
  collapsed?: boolean;
  counts?: NavCounts;
  /**
   * Spec K5: whether this deployment has a usable knowledge-graph (a Postgres
   * graph store). The "Memory" row is hidden when absent — showing it where the
   * graph is structurally always-empty (community-on-SQLite) would read as the
   * user having no memories. Availability is a RUNTIME signal from the API
   * (the window's `available` flag), not a build-time edition check, so a
   * self-hosted community deploy backed by Postgres still surfaces Memory.
   */
  memoryAvailable?: boolean;
}) {
  const pathname = usePathname();
  const t = useTranslations("nav");
  const items = ITEMS.filter(
    (item) => item.key !== "memory" || memoryAvailable,
  );
  return (
    <nav aria-label={t("primary")} className="flex flex-col gap-1">
      {items.map(({ href, key, icon: Icon, count }) => {
        const active = pathname === href || pathname.startsWith(`${href}/`);
        const countValue = count ? counts?.[count] : undefined;
        const link = (
          <Link
            href={href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            aria-label={collapsed ? t(key) : undefined}
            className={cn(
              "flex items-center rounded-md text-sm font-medium outline-none transition-colors duration-[var(--motion-duration-fast)] focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none",
              collapsed ? "size-9 justify-center mx-auto" : "gap-3 px-3 py-2",
              active
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground",
            )}
          >
            <Icon className="size-4 shrink-0" />
            {collapsed ? null : t(key)}
            {!collapsed && countValue !== undefined && countValue > 0 ? (
              <span className="ml-auto type-caption normal-case tracking-normal text-muted-foreground tabular-nums">
                {countValue}
              </span>
            ) : null}
          </Link>
        );

        if (collapsed) {
          return (
            <Tooltip key={href}>
              <TooltipTrigger render={link} />
              <TooltipContent side="right">{t(key)}</TooltipContent>
            </Tooltip>
          );
        }
        return <span key={href}>{link}</span>;
      })}
    </nav>
  );
}
