"use client";

import { Dialog } from "@base-ui/react/dialog";
import { ImageUp, Pencil, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import { useAuth } from "@/auth";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { Button, buttonVariants } from "@/components/ui/button";
import { useApi } from "@/lib/api/use-api";
import { uploadImage } from "@/lib/upload";
import { cn } from "@/lib/utils";

/** How long we poll for the async regeneration before giving up (bounded). */
const REGEN_POLL_MS = 3_000;
const REGEN_POLL_MAX = 20; // ≈ 60s

/**
 * R11-B6 rider (owner-ruled) — the kit's pencil-on-avatar: a MODAL for
 * replacing the image (upload) or REGENERATING it, wired end-to-end:
 * `POST /v1/personas/{id}/avatar/regenerate` (202 — the same queue/inline
 * doors the create path uses), then a bounded poll of the persona until
 * `avatar_url` changes. Every state is honest — uploading, regenerating,
 * the no-change timeout.
 */
export function AvatarModal({
  personaId,
  name,
  avatarUrl,
  onChange,
}: {
  personaId: string;
  name: string;
  avatarUrl: string | null;
  onChange: (url: string | null) => void;
}) {
  const t = useTranslations("personaPage.avatar");
  const { getToken } = useAuth();
  const api = useApi();
  const inputId = useId();
  const [uploading, setUploading] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    },
    [],
  );

  async function handleFile(file: File) {
    setUploading(true);
    setError(null);
    try {
      const res = await uploadImage(personaId, file, {
        getToken: () => getToken(),
      });
      onChange(res.workspace_path);
    } catch {
      setError(t("uploadFailed"));
    } finally {
      setUploading(false);
    }
  }

  async function regenerate() {
    setRegenerating(true);
    setError(null);
    const before = avatarUrl;
    try {
      const res = await api.POST(
        "/v1/personas/{persona_id}/avatar/regenerate",
        {
          params: { path: { persona_id: personaId } },
        },
      );
      if (res.error !== undefined) throw new Error("regenerate failed");
      // Bounded poll: the generation is async (queue or background task) —
      // the durable persona row is the only honest oracle for the new image.
      let attempts = 0;
      const poll = async () => {
        attempts += 1;
        const detail = await api.GET("/v1/personas/{persona_id}", {
          params: { path: { persona_id: personaId } },
        });
        const url = detail.data?.avatar_url ?? null;
        if (url && url !== before) {
          onChange(url);
          setRegenerating(false);
          return;
        }
        if (attempts >= REGEN_POLL_MAX) {
          setRegenerating(false);
          setError(t("regenTimeout"));
          return;
        }
        pollTimer.current = setTimeout(() => void poll(), REGEN_POLL_MS);
      };
      pollTimer.current = setTimeout(() => void poll(), REGEN_POLL_MS);
    } catch {
      setRegenerating(false);
      setError(t("regenFailed"));
    }
  }

  return (
    <Dialog.Root>
      <Dialog.Trigger
        render={
          <button
            type="button"
            className="group relative shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={t("edit")}
          >
            <PersonaAvatar
              persona={{ id: personaId, name, avatar_url: avatarUrl }}
              size="lg"
            />
            <span className="absolute right-0 bottom-0 grid size-6 place-items-center rounded-full border border-border bg-background text-muted-foreground shadow-sm transition-colors group-hover:text-foreground">
              <Pencil className="size-3" aria-hidden="true" />
            </span>
          </button>
        }
      />
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-4 rounded-2xl border border-border bg-card p-6 shadow-xl outline-none">
          <Dialog.Title className="font-heading text-lg font-semibold">
            {t("title")}
          </Dialog.Title>
          <div className="flex items-center justify-center py-2">
            <PersonaAvatar
              persona={{ id: personaId, name, avatar_url: avatarUrl }}
              size="lg"
            />
          </div>
          <div className="flex flex-col gap-2">
            <label
              htmlFor={inputId}
              className={cn(
                buttonVariants({ variant: "outline" }),
                "w-full cursor-pointer gap-1.5",
                (uploading || regenerating) && "pointer-events-none opacity-50",
              )}
            >
              <ImageUp className="size-4" aria-hidden="true" />
              {uploading ? t("uploading") : t("replace")}
            </label>
            <input
              id={inputId}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="sr-only"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void handleFile(file);
                e.target.value = "";
              }}
            />
            <Button
              type="button"
              variant="outline"
              className="w-full gap-1.5"
              disabled={uploading || regenerating}
              onClick={() => void regenerate()}
            >
              <RefreshCw
                className={cn("size-4", regenerating && "animate-spin")}
                aria-hidden="true"
              />
              {regenerating ? t("regenerating") : t("regenerate")}
            </Button>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
          <p className="text-xs text-muted-foreground">{t("regenHint")}</p>
          <Dialog.Close
            render={
              <Button type="button" variant="ghost" className="w-full">
                {t("done")}
              </Button>
            }
          />
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
