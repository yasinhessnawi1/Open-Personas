import { Input as InputPrimitive } from "@base-ui/react/input";
import type * as React from "react";

import { cn } from "@/lib/utils";

// F2 T10 retokenise (D-F2-1): bare `transition-colors` → explicit
// --motion-duration-fast (focus/hover state transitions). `text-base md:text-sm`
// stays as deliberate iOS-input-zoom-prevention pattern (16px at mobile widths
// prevents Safari's auto-zoom on focus) — DESIGN.md tracks the rationale.
const INPUT_CLASS =
  "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base transition-colors duration-[var(--motion-duration-fast)] outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40";

/**
 * R9-014 (c) — a reusable icon-left adornment slot so search-style inputs
 * render the icon INSIDE the field's left padding (the standard search
 * treatment) rather than each call absolutely-positioning its own icon
 * beside the field. When `startIcon` is set the input is wrapped in a
 * relative container: the icon sits in the left padding (`pl-8`) and is
 * pointer-events-none so clicks/focus still land on the field.
 */
function Input({
  className,
  type,
  startIcon,
  ...props
}: React.ComponentProps<"input"> & { startIcon?: React.ReactNode }) {
  if (startIcon) {
    return (
      <div className="relative flex w-full items-center">
        <span
          data-slot="input-icon"
          aria-hidden="true"
          className="pointer-events-none absolute left-2.5 flex items-center text-muted-foreground [&_svg]:size-4"
        >
          {startIcon}
        </span>
        <InputPrimitive
          type={type}
          data-slot="input"
          className={cn(INPUT_CLASS, "pl-8", className)}
          {...props}
        />
      </div>
    );
  }
  return (
    <InputPrimitive
      type={type}
      data-slot="input"
      className={cn(INPUT_CLASS, className)}
      {...props}
    />
  );
}

export { Input };
