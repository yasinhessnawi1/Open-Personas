"use client";

import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { type AvatarPersona, PersonaAvatar } from "./persona-avatar";

/**
 * R9-014 (b) — reusable persona picker.
 *
 * A presentation-agnostic dropdown that lists the owner's personas (each row =
 * `<PersonaAvatar>` + name) and hands the chosen persona's id back through
 * `onSelect`. It says NOTHING about what selection does: the conversations page
 * passes an `onSelect` that creates a conversation + navigates to `/chat/{id}`;
 * future callers pass their own (e.g. attach a persona to a task).
 *
 * Built on the app's `<DropdownMenu>` primitive (base-ui Menu), so keyboard
 * open/roving-focus/Enter-select + labelling come for free.
 *
 * Persona shape is `AvatarPersona` — the existing minimum shape `<PersonaAvatar>`
 * consumes (compatible with the conversations page's `ConversationListPersona`
 * and the sidebar/persona-summary rows); no new type is invented.
 */
export interface PersonaPickerProps {
  /** The owner's personas to choose from. */
  personas: readonly AvatarPersona[];
  /** Called with the chosen persona's id. The caller decides what happens. */
  onSelect: (personaId: string) => void;
  /**
   * Optional custom trigger element (rendered via base-ui's `render` slot).
   * When omitted, a default outline "+ {label}" button is rendered.
   */
  trigger?: ReactNode;
  /** Accessible label / default-trigger text. Falls back to a generic string. */
  label?: string;
  className?: string;
}

export function PersonaPicker({
  personas,
  onSelect,
  trigger,
  label,
  className,
}: PersonaPickerProps) {
  const t = useTranslations("personaPicker");
  const triggerLabel = label ?? t("choosePersona");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={triggerLabel}
        data-slot="persona-picker-trigger"
        render={
          trigger !== undefined ? (
            (trigger as React.ReactElement)
          ) : (
            <button
              type="button"
              className={cn("v-btn v-btn--outline", className)}
            >
              <Plus className="size-4" aria-hidden="true" />
              {triggerLabel}
            </button>
          )
        }
      />
      <DropdownMenuContent
        align="end"
        className="max-h-80 min-w-56 overflow-y-auto"
        data-slot="persona-picker-content"
      >
        {personas.length === 0 ? (
          <div
            className="px-2 py-3 text-center text-sm text-muted-foreground"
            data-slot="persona-picker-empty"
          >
            {t("empty")}
          </div>
        ) : (
          personas.map((p) => (
            <DropdownMenuItem
              key={p.id}
              onClick={() => onSelect(p.id)}
              data-slot="persona-picker-item"
              className="gap-2"
            >
              <PersonaAvatar persona={p} size="sm" />
              <span className="truncate">{p.name}</span>
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
