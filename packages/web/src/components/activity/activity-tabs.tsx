"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

/**
 * The Activity area's sub-navigation (Spec A6, A6-D-1) — one area, three siblings.
 *
 * Review is the landing; Tasks + Approvals are peers within the area (not top-level nav rows). This
 * is the A6-D-1 re-home the W2 interim "Activity → /tasks" entry stood in for — now a single top-
 * level "Activity" row lands here on Review, and `/runs` is demoted to the task-detail drill.
 */
// R11-B1 (D-R11-4): the area consolidated under /activity — Review lands on the
// area root, Tasks + Approvals are child routes. Old standalone routes redirect.
const TABS = [
  { href: "/activity", key: "review" },
  { href: "/activity/tasks", key: "tasks" },
  { href: "/activity/approvals", key: "approvals" },
] as const;

export function ActivityTabs() {
  const t = useTranslations("activity");
  const pathname = usePathname();
  return (
    <nav
      aria-label={t("area")}
      className="mb-6 flex gap-1 border-b border-border"
    >
      {TABS.map(({ href, key }) => {
        // Review sits on the area ROOT, so it matches exactly — a prefix match
        // would light it up on /activity/tasks too. The child tabs keep the
        // prefix match (task detail keeps Tasks active).
        const active =
          href === "/activity"
            ? pathname === href
            : pathname === href || pathname.startsWith(`${href}/`);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm transition-colors",
              active
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t(key)}
          </Link>
        );
      })}
    </nav>
  );
}
