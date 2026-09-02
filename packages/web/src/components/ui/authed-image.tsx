"use client";

import { ImageOff } from "lucide-react";
import { useTranslations } from "next-intl";
import { useAuthedImageBlobUrl } from "@/lib/hooks/use-authed-image-blob-url";
import { cn } from "@/lib/utils";

/**
 * F2 primitive — `<AuthedImage>` (promoted by F4 T16
 * D-F4-X-authedimage-f2-promotion).
 *
 * Originally landed at `packages/web/src/components/chat/authed-image.tsx`
 * for F3 T10 (D-F3-X-image-serve-auth) as F3-local; F4 was the
 * second-consumer (`<InlineVisual>` T05 + `<ImageLightbox>` T12 +
 * existing `<MessageElement>` user-image render path), triggering the
 * strangler-fig promotion per D-F3-X-preview-placement /
 * chip-placement extract-when-second-consumer pattern.
 *
 * The canonical home is now `src/components/ui/authed-image.tsx` (this
 * file). A re-export shim lives at `src/components/chat/authed-image.tsx`
 * so existing callers see no churn.
 *
 * Wraps `useAuthedImageBlobUrl` and the three error/loading affordances
 * the hook produces:
 *   - loading → muted skeleton box
 *   - 404 → "image unavailable" placeholder with the persona-detail icon
 *   - 5xx → red ring + alt text (no inline retry; the parent component
 *           can re-mount this by changing the ref to force a fresh fetch)
 */
export interface AuthedImageProps {
  personaId: string;
  workspacePath: string;
  mediaType: string;
  /** Accessible alt text — required for screen readers. */
  alt: string;
  className?: string;
}

export function AuthedImage({
  personaId,
  workspacePath,
  alt,
  className,
}: AuthedImageProps) {
  const t = useTranslations("chat.composer.upload");
  const { src, loading, error } = useAuthedImageBlobUrl(
    personaId,
    workspacePath,
  );

  if (loading && !src) {
    return (
      <div
        className={cn("size-32 animate-pulse rounded-md bg-muted", className)}
        role="img"
        aria-label={t("uploading", { filename: alt })}
      />
    );
  }

  if (error) {
    return (
      <div
        className={cn(
          "grid size-32 place-items-center rounded-md bg-muted",
          "text-muted-foreground ring-2 ring-destructive",
          className,
        )}
        role="alert"
        aria-label={t("failed", { reason: error.message })}
      >
        <ImageOff className="size-6" aria-hidden />
      </div>
    );
  }

  if (!src) {
    return (
      <div
        className={cn(
          "grid size-32 place-items-center rounded-md bg-muted",
          "text-muted-foreground",
          className,
        )}
        role="img"
        aria-label={t("unavailable")}
      >
        <ImageOff className="size-6" aria-hidden />
      </div>
    );
  }

  return (
    // biome-ignore lint/performance/noImgElement: blob: URLs can't go through next/image
    <img
      src={src}
      alt={alt}
      className={cn("max-w-xs rounded-md object-cover", className)}
    />
  );
}
