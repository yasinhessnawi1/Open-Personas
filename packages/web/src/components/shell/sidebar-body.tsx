"use client";

import { Plus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { buttonVariants } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { TooltipProvider } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { AccountMenu } from "./account-menu";
import { CommandTrigger } from "./command-palette";
import { Nav } from "./nav";
import type { SidebarData } from "./sidebar-data";
import { AllChatsLink, MessagesList, PersonasRail } from "./sidebar-sections";

/**
 * Shared inner content for the MOBILE sheet. Mirrors the desktop section model
 * (New persona · Nav · PERSONAS · MESSAGES · pinned Settings) but always in the
 * expanded layout — the sheet has no resize/collapse affordance. Desktop uses
 * the richer `<Sidebar>` (resize + collapse) directly.
 */
export function SidebarBody({
  data,
  onNavigate,
}: {
  data: SidebarData;
  onNavigate?: () => void;
}) {
  const t = useTranslations("nav");

  return (
    <TooltipProvider>
      <div className="flex min-h-0 flex-1 flex-col gap-4">
        {/* ⌘K command / search (Spec 35 D-35-14). */}
        <CommandTrigger />

        <Link
          href="/personas/new"
          onClick={onNavigate}
          className={cn(buttonVariants(), "justify-start gap-2")}
        >
          <Plus className="size-4" />
          {t("newPersona")}
        </Link>

        <Nav
          onNavigate={onNavigate}
          counts={{
            personas: data.counts.personas,
            calls: data.counts.calls,
            activity: data.counts.activeTasks,
            memory: data.counts.memoryNodes,
            schedule: data.counts.schedules,
          }}
          memoryAvailable={data.memoryAvailable}
        />

        <Separator className="bg-sidebar-border" />

        <section className="flex flex-col gap-1.5">
          <h2 className="px-2 type-caption text-muted-foreground">
            {t("sidebar.personas")}
          </h2>
          <PersonasRail
            personas={data.personas}
            collapsed={false}
            onNavigate={onNavigate}
          />
        </section>

        <section className="flex min-h-0 flex-1 flex-col gap-1.5">
          {/* R9-009: the Conversations nav row folded into MESSAGES — the
              header carries the "All chats (N)" link to /conversations. */}
          <div className="flex items-baseline justify-between gap-2 px-2">
            <h2 className="type-caption text-muted-foreground">
              {t("sidebar.messages")}
            </h2>
            <AllChatsLink
              count={data.counts.conversations}
              onNavigate={onNavigate}
            />
          </div>
          <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1">
            <MessagesList
              conversations={data.conversations}
              collapsed={false}
              onNavigate={onNavigate}
            />
          </div>
        </section>

        <div className="mt-auto">
          <Separator className="mb-2 bg-sidebar-border" />
          {/* Account footer — custom account menu (Spec 35 D-35-16). */}
          <AccountMenu name={data.ownerName} />
        </div>
      </div>
    </TooltipProvider>
  );
}
