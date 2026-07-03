"use client";

/**
 * The custom v1 account menu (Spec 35 D-35-16) — the redesign's own account
 * chrome in the sidebar footer, NOT Clerk's prebuilt <UserButton/> dropdown.
 *
 * Edition-agnostic + Clerk-free: it reads the account shape from `useAccount()`
 * (the `@/auth` façade — Clerk in cloud, a degraded stub in community) and
 * imports NO `@clerk/*`, so it stays in the community-reachable graph and
 * `check:clerk-free` stays green. Cloud shows name/avatar + manage-account +
 * sign-out; community degrades to settings + appearance only.
 */

import {
  Cable,
  LogOut,
  Monitor,
  Moon,
  Settings,
  Sun,
  UserCog,
} from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useAccount } from "@/auth";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

/** Two-letter initials from a display name (empty when unknown). */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "";
  return (parts[0][0] + (parts[1]?.[0] ?? "")).toUpperCase();
}

export function AccountMenu({ collapsed = false }: { collapsed?: boolean }) {
  const account = useAccount();
  const t = useTranslations("nav.account");
  const tn = useTranslations("nav");
  const tt = useTranslations("theme");
  const { setTheme } = useTheme();

  const label = account.name || t("menu");
  const sub = account.email || t("plan");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={t("menu")}
        className={cn("v-acct w-full", collapsed && "justify-center")}
      >
        <span className="v-acct__av" aria-hidden>
          {account.imageUrl ? (
            // Clerk public CDN avatar; plain <img> (no auth header, no next/image
            // remote-domain config needed). Falls back to initials when absent.
            // biome-ignore lint/performance/noImgElement: external Clerk CDN avatar — next/image would require remote-domain config for no optimisation benefit on a 28px avatar.
            <img
              src={account.imageUrl}
              alt=""
              className="size-full rounded-full object-cover"
            />
          ) : (
            initials(label)
          )}
        </span>
        {!collapsed && (
          <span className="flex min-w-0 flex-col text-left">
            <span className="v-acct__name truncate">{label}</span>
            <span className="v-acct__plan truncate">{sub}</span>
          </span>
        )}
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" side="top" className="w-56">
        {account.available && account.name ? (
          <>
            <DropdownMenuGroup>
              <DropdownMenuLabel className="flex flex-col gap-0.5">
                <span className="truncate text-foreground">{account.name}</span>
                {account.email ? (
                  <span className="truncate font-normal text-muted-foreground">
                    {account.email}
                  </span>
                ) : null}
              </DropdownMenuLabel>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
          </>
        ) : null}

        {account.manageAccount ? (
          <DropdownMenuItem onClick={account.manageAccount}>
            <UserCog />
            {t("manageAccount")}
          </DropdownMenuItem>
        ) : null}

        <DropdownMenuItem render={<Link href="/settings" />}>
          <Settings />
          {tn("settings")}
        </DropdownMenuItem>

        <DropdownMenuItem render={<Link href="/settings/connectors" />}>
          <Cable />
          {tn("connectors")}
        </DropdownMenuItem>

        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <Sun className="dark:hidden" />
            <Moon className="hidden dark:block" />
            {t("appearance")}
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuItem onClick={() => setTheme("light")}>
              <Sun />
              {tt("light")}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme("dark")}>
              <Moon />
              {tt("dark")}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme("system")}>
              <Monitor />
              {tt("system")}
            </DropdownMenuItem>
          </DropdownMenuSubContent>
        </DropdownMenuSub>

        {account.signOut ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={account.signOut}>
              <LogOut />
              {t("signOut")}
            </DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
