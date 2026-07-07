"use client";

import {
  Activity,
  CalendarClock,
  Home,
  ListChecks,
  MessagesSquare,
  Phone,
  ShieldCheck,
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
  { href: "/", key: "home", icon: Home, count: undefined },
  { href: "/personas", key: "personas", icon: Sparkles, count: "personas" },
  {
    href: "/conversations",
    key: "conversations",
    icon: MessagesSquare,
    count: "conversations",
  },
  // Spec V9: the voice-call history surface.
  { href: "/calls", key: "calls", icon: Phone, count: undefined },
  { href: "/runs", key: "tasks", icon: ListChecks, count: undefined },
  // Spec A6 (W2): the autonomy Tasks list — the cross-persona state matrix. Interim entry under
  // the area name "Activity" (A6-D-1); the full Activity re-home (Review landing + Tasks/Approvals
  // siblings, folding /approvals in) lands with W5.
  { href: "/tasks", key: "activity", icon: Activity, count: undefined },
  // Spec A6 (W4): the approvals inbox — pending decisions across tasks, the chat-twin's surface.
  { href: "/approvals", key: "approvals", icon: ShieldCheck, count: undefined },
  // Spec K5: the interactive knowledge-graph — "what your personas know, yours to shape."
  { href: "/memory", key: "memory", icon: Waypoints, count: undefined },
  // Spec A8: the schedule/calendar surface (time's view of the personas'
  // commitments). The route + calendar shipped styled but was unreachable —
  // reachable only by typed URL — until this nav row (R4-C1-10, built-but-inert
  // at the nav level). A6 may later re-home it alongside the review inbox.
  { href: "/schedule", key: "schedule", icon: CalendarClock, count: undefined },
] as const;

/** Live counts shown on nav rows (Spec 35 D-35-13) — derived from sidebar data. */
export interface NavCounts {
  readonly personas?: number;
  readonly conversations?: number;
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
