"use client";

/**
 * The desktop app-sidebar: a resizable, collapsible, scrollable chrome built on
 * a small SECTION MODEL so more nav sections can be added later without rework.
 *
 * Layout contract (top → bottom):
 *   1. Header row     — brand + collapse toggle (fixed).
 *   2. New persona    — the primary action (fixed).
 *   3. Nav            — primary links (fixed).
 *   4. PERSONAS       — compact recent-persona rail (fixed).
 *   5. MESSAGES       — recent conversations; the FLEXIBLE region that grows to
 *                       fill remaining height and scrolls.
 *   6. Settings       — pinned to the very bottom (fixed).
 *
 * A new section is added by dropping another `<SidebarSection>` between (4) and
 * (5) (fixed) or by composing into the flexible middle. The pinned-bottom slot
 * is reserved for Settings.
 *
 * Resize: a vertical drag handle with WAI-ARIA `separator` semantics (focusable,
 * arrow/Home/End adjustable). Collapse: an icon-rail toggle. Both states persist
 * to localStorage and restore SSR-safely (the default renders on the server +
 * first paint; persisted values are read post-hydration — see usePersistedState).
 */

import { ChevronsLeft, ChevronsRight, Plus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useRef } from "react";
import { buttonVariants } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { usePersistedState } from "@/lib/hooks/use-persisted-state";
import { cn } from "@/lib/utils";
import { AccountMenu } from "./account-menu";
import { Brand } from "./brand";
import { CommandTrigger } from "./command-palette";
import { Nav } from "./nav";
import { NotificationBell } from "./notification-bell";
import type { SidebarData } from "./sidebar-data";
import { MessagesList, PersonasRail } from "./sidebar-sections";

/** Width bounds + the default (px). Collapsed snaps to the icon rail. */
const MIN_WIDTH = 224;
const MAX_WIDTH = 420;
const DEFAULT_WIDTH = 256;
const RAIL_WIDTH = 64;
const STEP = 16;

const WIDTH_KEY = "persona:sidebar-width";
const COLLAPSED_KEY = "persona:sidebar-collapsed";

function clampWidth(value: number): number {
  if (Number.isNaN(value)) return DEFAULT_WIDTH;
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.round(value)));
}

/**
 * A sidebar section: an optional heading + a body. The reusable unit of the
 * section model. `grow` makes the section the flexible, scrolling region
 * (only the MESSAGES section uses it). `collapsed` hides the textual heading
 * in icon-rail mode.
 */
function SidebarSection({
  heading,
  collapsed,
  grow,
  children,
}: {
  heading?: string;
  collapsed: boolean;
  grow?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section
      className={cn("flex flex-col gap-1.5", grow && "min-h-0 flex-1")}
      data-slot="sidebar-section"
    >
      {heading && !collapsed ? (
        <h2 className="px-2 type-caption text-muted-foreground">{heading}</h2>
      ) : null}
      {grow ? (
        <ScrollArea className="-mx-1 min-h-0 flex-1">
          <div className="px-1">{children}</div>
        </ScrollArea>
      ) : (
        children
      )}
    </section>
  );
}

export function Sidebar({ data }: { data: SidebarData }) {
  const t = useTranslations("nav");

  const [width, setWidth] = usePersistedState<number>(WIDTH_KEY, {
    fallback: DEFAULT_WIDTH,
    parse: (raw) => clampWidth(Number(raw)),
    serialize: String,
  });
  const [collapsed, setCollapsed] = usePersistedState<boolean>(COLLAPSED_KEY, {
    fallback: false,
    parse: (raw) => raw === "true",
    serialize: String,
  });

  const asideRef = useRef<HTMLElement | null>(null);
  const dragging = useRef(false);

  // --- Pointer-drag resize ------------------------------------------------
  const onPointerMove = useCallback(
    (event: PointerEvent) => {
      if (!dragging.current || !asideRef.current) return;
      const left = asideRef.current.getBoundingClientRect().left;
      setWidth(clampWidth(event.clientX - left));
    },
    [setWidth],
  );

  const stopDrag = useCallback(() => {
    dragging.current = false;
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", stopDrag);
    document.body.style.removeProperty("cursor");
    document.body.style.removeProperty("user-select");
  }, [onPointerMove]);

  const startDrag = useCallback(
    (event: React.PointerEvent) => {
      if (collapsed) return;
      event.preventDefault();
      dragging.current = true;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      document.addEventListener("pointermove", onPointerMove);
      document.addEventListener("pointerup", stopDrag);
    },
    [collapsed, onPointerMove, stopDrag],
  );

  // --- Keyboard resize (ARIA separator) -----------------------------------
  const onSeparatorKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (collapsed) return;
      switch (event.key) {
        case "ArrowLeft":
          event.preventDefault();
          setWidth(clampWidth(width - STEP));
          break;
        case "ArrowRight":
          event.preventDefault();
          setWidth(clampWidth(width + STEP));
          break;
        case "Home":
          event.preventDefault();
          setWidth(MIN_WIDTH);
          break;
        case "End":
          event.preventDefault();
          setWidth(MAX_WIDTH);
          break;
        default:
          break;
      }
    },
    [collapsed, width, setWidth],
  );

  const effectiveWidth = collapsed ? RAIL_WIDTH : width;

  return (
    <TooltipProvider>
      <aside
        ref={asideRef}
        aria-label={t("primary")}
        data-slot="app-shell-sidebar"
        data-collapsed={collapsed}
        style={{ width: `${effectiveWidth}px` }}
        className={cn(
          // A fixed full-viewport-height column: sticky to the top so it never
          // scrolls with the page, and capped at the (small) viewport height so
          // it never grows taller than the screen. This height ceiling is what
          // lets the MESSAGES region's `min-h-0 flex-1` ScrollArea scroll
          // internally instead of pushing the pinned Settings footer off-screen.
          "relative hidden shrink-0 border-r border-sidebar-border bg-sidebar md:sticky md:top-0 md:flex md:h-svh md:flex-col",
          "transition-[width] duration-[var(--motion-duration-normal)] ease-[var(--motion-ease-standard)] motion-reduce:transition-none",
        )}
      >
        <div className="flex min-h-0 flex-1 flex-col gap-4 p-3">
          {/* (1) Header: brand + notification bell (Spec 35 L) + collapse. */}
          <div
            className={cn(
              "flex items-center",
              collapsed ? "justify-center" : "justify-between",
            )}
          >
            {collapsed ? null : <Brand className="min-w-0 px-1" />}
            <div
              className={cn("flex items-center gap-1", collapsed && "flex-col")}
            >
              <NotificationBell />
              <CollapseToggle
                collapsed={collapsed}
                onToggle={() => setCollapsed(!collapsed)}
                label={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
              />
            </div>
          </div>

          {/* (2) ⌘K command / search (Spec 35 D-35-14). */}
          <CommandTrigger collapsed={collapsed} />

          {/* (3) Primary action. */}
          <NewPersonaButton collapsed={collapsed} label={t("newPersona")} />

          {/* (4) Primary nav, with live counts (Spec 35 D-35-13). */}
          <Nav
            collapsed={collapsed}
            counts={{
              personas: data.personas.length,
              conversations: data.conversations.length,
            }}
            memoryAvailable={data.memoryAvailable}
          />

          <Separator className="bg-sidebar-border" />

          {/* (4) PERSONAS — fixed compact rail. */}
          <SidebarSection heading={t("sidebar.personas")} collapsed={collapsed}>
            <PersonasRail personas={data.personas} collapsed={collapsed} />
          </SidebarSection>

          {/* (5) MESSAGES — the flexible, growing, scrolling region.
              (R4 T2: the CALLS recent-list preview was removed from the sidebar
              — the Calls *nav item* stays and the full history lives at /calls.) */}
          <SidebarSection
            heading={t("sidebar.messages")}
            collapsed={collapsed}
            grow
          >
            <MessagesList
              conversations={data.conversations}
              collapsed={collapsed}
            />
          </SidebarSection>

          {/* (7) Account footer — the custom account menu (Spec 35 D-35-16:
           * avatar + name + settings/appearance/sign-out), moved out of the
           * top-right header island into a persistent home. `shrink-0` keeps it
           * at natural height while the MESSAGES region above absorbs the slack
           * and scrolls internally. */}
          <div className="mt-auto shrink-0">
            <Separator className="mb-2 bg-sidebar-border" />
            <AccountMenu collapsed={collapsed} name={data.ownerName} />
          </div>
        </div>

        {/* Resize handle — a WAI-ARIA window-splitter (focusable, keyboard-
         * adjustable `separator`). biome's useSemanticElements suggests <hr>,
         * but a splitter must be focusable + carry aria-valuenow/min/max +
         * key handlers, which <hr> cannot — the role here is correct. */}
        {/* biome-ignore lint/a11y/useSemanticElements: an interactive resize splitter cannot be an <hr>. */}
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label={t("sidebar.resize")}
          aria-valuemin={MIN_WIDTH}
          aria-valuemax={MAX_WIDTH}
          aria-valuenow={collapsed ? RAIL_WIDTH : width}
          tabIndex={collapsed ? -1 : 0}
          aria-hidden={collapsed}
          onPointerDown={startDrag}
          onKeyDown={onSeparatorKeyDown}
          className={cn(
            "group/resize absolute inset-y-0 -right-1 z-10 w-2 cursor-col-resize touch-none outline-none",
            collapsed && "pointer-events-none hidden",
          )}
        >
          <span
            className="absolute inset-y-0 right-1 w-px bg-transparent transition-colors duration-[var(--motion-duration-fast)] group-hover/resize:bg-sidebar-ring group-focus-visible/resize:bg-sidebar-ring motion-reduce:transition-none"
            aria-hidden
          />
        </div>
      </aside>
    </TooltipProvider>
  );
}

function CollapseToggle({
  collapsed,
  onToggle,
  label,
}: {
  collapsed: boolean;
  onToggle: () => void;
  label: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            onClick={onToggle}
            aria-label={label}
            aria-pressed={collapsed}
            className={cn(
              buttonVariants({ variant: "ghost", size: "icon-sm" }),
              "shrink-0 text-sidebar-foreground",
            )}
          />
        }
      >
        {collapsed ? (
          <ChevronsRight className="size-4" />
        ) : (
          <ChevronsLeft className="size-4" />
        )}
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

function NewPersonaButton({
  collapsed,
  label,
}: {
  collapsed: boolean;
  label: string;
}) {
  if (collapsed) {
    return (
      <Tooltip>
        <TooltipTrigger
          render={
            <Link
              href="/personas/new"
              aria-label={label}
              className={cn(buttonVariants({ size: "icon" }), "mx-auto")}
            />
          }
        >
          <Plus className="size-4" />
        </TooltipTrigger>
        <TooltipContent side="right">{label}</TooltipContent>
      </Tooltip>
    );
  }
  return (
    <Link
      href="/personas/new"
      className={cn(buttonVariants(), "justify-start gap-2")}
    >
      <Plus className="size-4" />
      {label}
    </Link>
  );
}
