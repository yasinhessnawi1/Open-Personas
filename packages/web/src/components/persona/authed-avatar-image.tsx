"use client";

import type { CSSProperties, ReactNode } from "react";
import { useAuthedImageBlobUrl } from "@/lib/hooks/use-authed-image-blob-url";
import { cn } from "@/lib/utils";

/**
 * Renders a persona avatar that lives behind the authed uploads route
 * (`GET /v1/personas/:id/uploads/:ref`, Bearer + RLS).
 *
 * A raw `<img src>` cannot load that endpoint — the browser sends no
 * Authorization header and resolves a relative path against the web origin
 * (→ 404). So generated/auto-avatar refs go through `useAuthedImageBlobUrl`
 * (fetch-with-Bearer → blob URL), exactly like chat images
 * (`<AuthedImage>`). Until the blob resolves — and on 404 / error — the
 * `fallback` (the persona's initials mark) renders, so the avatar surface
 * never shows a broken image; the letters → portrait swap is the intended
 * F1 transition (D-F1-2).
 *
 * External / data / blob avatar URLs do NOT use this — `<PersonaAvatar>`
 * renders those with a plain `<img>` (they're directly loadable).
 */
export function AuthedAvatarImage({
  personaId,
  workspacePath,
  alt = "",
  wrapperClassName,
  style,
  fallback,
}: {
  personaId: string;
  /** Workspace-relative ref, e.g. `uploads/<blake2b>.png`. */
  workspacePath: string;
  alt?: string;
  wrapperClassName?: string;
  style?: CSSProperties;
  /** Rendered while loading and on 404 / error (the initials mark). */
  fallback: ReactNode;
}) {
  const { src } = useAuthedImageBlobUrl(personaId, workspacePath);

  if (!src) {
    // Loading, 404, or error → graceful initials fallback (never a broken img).
    return <>{fallback}</>;
  }

  return (
    <span
      style={style}
      className={cn(
        "inline-block overflow-hidden rounded-full",
        wrapperClassName,
      )}
    >
      {/* biome-ignore lint/performance/noImgElement: blob: URLs can't go through next/image */}
      <img
        src={src}
        alt={alt}
        // R9-036 REOPEN #2: img is draggable BY DEFAULT in every real
        // browser, independent of whatever the wrapping Link sets — see
        // use-press-swipe-gesture.ts's handlers doc for why the ancestor's
        // draggable/onDragStart guard alone isn't enough for a nested img.
        draggable={false}
        className="size-full object-cover"
      />
    </span>
  );
}
