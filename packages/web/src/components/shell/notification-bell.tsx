"use client";

/**
 * Spec 35 cluster L (D-35-11) + Spec P6 (D4-e) — the notification bell + center.
 *
 * Renders the UNION (D-P6-7) of two feeds, newest-first, in a base-ui Popover:
 *   - the client `useNotify()` feed (localStorage) — Spec 35's CRUD/chat toasts +
 *     P6's client-session low-balance advisory;
 *   - the durable, cross-device `useServerNotifications()` feed (run-terminal /
 *     persona-ready, server-authored).
 * Opening the panel marks everything read (both feeds); a deep-linked row routes +
 * marks itself read + closes the panel (P6-D-10). "Clear all" empties the client
 * feed and marks the durable feed read. The trigger carries an unread-count badge.
 */

import { Popover } from "@base-ui/react/popover";
import { Bell } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import {
  type NotifyLevel,
  useNotify,
} from "@/components/providers/notification-provider";
import { useServerNotifications } from "@/components/providers/server-notifications-provider";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Level → dot colour, all token-resolved (no-literals gate). The chart palette
 * carries the only multi-hue status tokens: chart-4 (green/h145), chart-2
 * (amber/h73), chart-3 (blue/h232); errors use the destructive token.
 */
const DOT_BY_LEVEL: Record<NotifyLevel, string> = {
  success: "bg-chart-4",
  error: "bg-destructive",
  warning: "bg-chart-2",
  info: "bg-chart-3",
};

/** A bell row normalised across the client + server feeds. */
interface BellItem {
  key: string;
  level: NotifyLevel;
  title: string;
  body?: string;
  href?: string;
  at: number;
  read: boolean;
  /** Mark this row's own feed read (client or server). */
  onActivate: () => void;
}

/** The dot + title/body/time content shared by linked and plain rows. */
function EntryContent({
  item,
  relativeTime,
}: {
  item: BellItem;
  relativeTime: string;
}) {
  return (
    <>
      <span
        aria-hidden
        className={cn(
          "mt-1.5 size-2 shrink-0 rounded-full",
          DOT_BY_LEVEL[item.level],
        )}
      />
      <div className="min-w-0 flex-1">
        <p className="font-medium text-foreground text-sm">{item.title}</p>
        {item.body ? (
          <p className="text-muted-foreground text-xs">{item.body}</p>
        ) : null}
        <p className="type-caption mt-0.5 text-muted-foreground">
          {relativeTime}
        </p>
      </div>
    </>
  );
}

export function NotificationBell({ className }: { className?: string }) {
  const t = useTranslations("notifications");
  const format = useFormatter();
  const client = useNotify();
  const server = useServerNotifications();

  // The union, newest-first. Keys are feed-prefixed so a client + server id can
  // never collide.
  const items: BellItem[] = [
    ...client.entries.map((e) => ({
      key: `c:${e.id}`,
      level: e.level,
      title: e.title,
      body: e.body,
      href: e.href,
      at: e.at,
      read: e.read,
      onActivate: () => client.markRead(e.id),
    })),
    ...server.entries.map((e) => ({
      key: `s:${e.id}`,
      level: e.level,
      title: e.title,
      href: e.href,
      at: e.at,
      read: e.read,
      onActivate: () => server.markRead(e.id),
    })),
  ].sort((a, b) => b.at - a.at);

  const unreadCount = client.unreadCount + server.unreadCount;

  const markAllRead = () => {
    client.markAllRead();
    server.markAllRead();
  };
  const clearAll = () => {
    client.clear();
    server.markAllRead();
  };

  return (
    <Popover.Root
      onOpenChange={(open) => {
        if (open) markAllRead();
      }}
    >
      <Popover.Trigger
        render={
          <Button variant="ghost" size="icon-sm" aria-label={t("open")} />
        }
        className={cn("relative", className)}
      >
        <Bell />
        {unreadCount > 0 ? (
          <>
            <span
              aria-hidden
              data-slot="notification-unread"
              className="type-caption -top-0.5 -right-0.5 absolute flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 font-medium text-primary-foreground"
            >
              {unreadCount > 9 ? "9+" : unreadCount}
            </span>
            <span className="sr-only">
              {t("unreadLabel", { count: unreadCount })}
            </span>
          </>
        ) : null}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner side="bottom" align="end" sideOffset={8}>
          <Popover.Popup
            data-slot="notification-center"
            className="z-50 flex max-h-[min(28rem,70vh)] w-[min(22rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-xl border bg-popover bg-clip-padding text-popover-foreground shadow-[var(--elevation-3)] transition duration-[var(--motion-duration-fast)] ease-[var(--motion-ease-standard)] data-ending-style:scale-95 data-ending-style:opacity-0 data-starting-style:scale-95 data-starting-style:opacity-0"
          >
            <div className="flex items-center justify-between border-b px-3 py-2">
              <Popover.Title className="font-heading font-medium text-foreground text-sm">
                {t("title")}
              </Popover.Title>
              {items.length > 0 ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={clearAll}
                  className="h-7 px-2 text-muted-foreground text-xs"
                >
                  {t("clear")}
                </Button>
              ) : null}
            </div>
            {items.length === 0 ? (
              <p className="px-3 py-8 text-center text-muted-foreground text-sm">
                {t("empty")}
              </p>
            ) : (
              <ul
                className="flex flex-col overflow-y-auto"
                data-slot="notification-feed"
              >
                {items.map((item) => {
                  const content = (
                    <EntryContent
                      item={item}
                      relativeTime={format.relativeTime(item.at, Date.now())}
                    />
                  );
                  return (
                    <li
                      key={item.key}
                      className="border-b last:border-b-0"
                      data-slot="notification-entry"
                      data-level={item.level}
                    >
                      {item.href ? (
                        // Deep-linked row (P6-D-10): routes to the target, marks
                        // this entry read, and closes the panel. Popover.Close
                        // renders the Link so it's a real anchor (keyboard-
                        // reachable, role=link) that dismisses on activate.
                        <Popover.Close
                          render={<Link href={item.href} />}
                          onClick={item.onActivate}
                          className="flex w-full gap-2 px-3 py-2 text-left outline-none transition-colors hover:bg-accent focus-visible:bg-accent"
                        >
                          {content}
                        </Popover.Close>
                      ) : (
                        <div className="flex gap-2 px-3 py-2">{content}</div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
