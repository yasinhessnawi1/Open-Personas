"use client";

import {
  CalendarClock,
  Home,
  ListChecks,
  MessagesSquare,
  Phone,
  Sparkles,
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
}: {
  onNavigate?: () => void;
  collapsed?: boolean;
  counts?: NavCounts;
}) {
  const pathname = usePathname();
  const t = useTranslations("nav");
  return (
    <nav aria-label={t("primary")} className="flex flex-col gap-1">
      {ITEMS.map(({ href, key, icon: Icon, count }) => {
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
