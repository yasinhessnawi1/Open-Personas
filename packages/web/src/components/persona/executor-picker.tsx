"use client";

import { ChevronDown } from "lucide-react";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { type AvatarPersona, PersonaAvatar } from "./persona-avatar";
import { PersonaPicker } from "./persona-picker";

/**
 * R11 (owner-ruled) — the ONE executor-selection control: the shared
 * `<PersonaPicker>` (R9-014's avatar+name dropdown, the same one behind "new
 * chat" and "new call") dressed as a form field. Every "who runs it?" surface
 * (New routine, Hand a task to a persona) uses THIS instead of a bare
 * `<select>` of names — a persona is an identity, not an option string.
 */
export function ExecutorPicker({
  personas,
  value,
  onSelect,
  label,
  placeholder,
}: {
  personas: readonly AvatarPersona[];
  /** The selected persona id; "" = nothing chosen yet. */
  value: string;
  onSelect: (personaId: string) => void;
  /** Accessible name for the trigger (the field's visible label text). */
  label: string;
  placeholder: string;
}) {
  const selected = personas.find((p) => p.id === value) ?? null;
  return (
    <PersonaPicker
      personas={personas}
      onSelect={onSelect}
      label={label}
      trigger={
        <button
          type="button"
          className="flex h-10 w-full items-center gap-2 rounded-md border border-border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-slot="executor-picker-trigger"
        >
          {selected ? (
            <>
              <span style={personaIdentityStyle(selected)}>
                <PersonaAvatar persona={selected} size="sm" />
              </span>
              <span className="truncate">{selected.name}</span>
            </>
          ) : (
            <span className="text-muted-foreground">{placeholder}</span>
          )}
          <ChevronDown
            className="ml-auto size-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
        </button>
      }
    />
  );
}
