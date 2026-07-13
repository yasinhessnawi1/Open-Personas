"use client";

/**
 * R9-029 — the desktop transcript right panel.
 *
 * Desktop-only projection of the call transcript (mounted by
 * `<VoiceCallSurface>` at `md:` and up, beside the call stage — mobile keeps
 * the existing compact `<VoiceCaptions>` treatment untouched, per the owner's
 * mockup: "current is good for mobile"). Same underlying `CaptionSegment[]`
 * data `<VoiceCaptions>` renders, but turns are attributed by AVATAR (reusing
 * chat's `<PersonaAvatar>` + `<UserAvatar>`, the R9-014 reuse discipline)
 * instead of the "You:" / "{Persona}:" text label, and the live-follow state
 * is surfaced as a visible AUTO chip instead of being purely internal.
 *
 * Auto-scroll/live-follow behaviour is the SAME pinned-to-bottom contract
 * `<VoiceCaptions>` implements: follows the newest turn while pinned; a
 * manual scroll-up unpins (the reader is re-reading); the AUTO chip shows the
 * current state and re-pins (jumps to latest) on click.
 */

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { UserAvatar } from "@/components/chat/user-avatar";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { Markdown } from "@/components/ui/markdown";
import { cn } from "@/lib/utils";
import type { CaptionSegment } from "@/lib/voice/captions";

export interface VoiceTranscriptPanelProps {
  captions: CaptionSegment[];
  persona: AvatarPersona;
}

export function VoiceTranscriptPanel({
  captions,
  persona,
}: VoiceTranscriptPanelProps): React.JSX.Element {
  const t = useTranslations("voice");
  const scrollRef = useRef<HTMLDivElement>(null);
  // Pinned = follow the newest turn (the SAME contract VoiceCaptions uses).
  const [pinned, setPinned] = useState(true);

  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll on every caption mutation
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinned) el.scrollTop = el.scrollHeight;
  }, [captions, pinned]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    setPinned(atBottom);
  };

  const jumpToLatest = () => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setPinned(true);
  };

  return (
    <div
      className="flex h-full min-h-0 flex-col"
      data-slot="voice-transcript-panel"
    >
      <header className="flex shrink-0 items-center justify-between border-border border-b px-4 py-3">
        <span className="type-ui font-medium">{t("transcript")}</span>
        <button
          type="button"
          onClick={jumpToLatest}
          aria-pressed={pinned}
          aria-label={t("autoFollow")}
          title={t("autoFollow")}
          data-slot="voice-transcript-auto-chip"
          data-state={pinned ? "active" : "inactive"}
          className={cn(
            "type-caption rounded-full border px-2 py-0.5 font-mono uppercase tracking-wide transition-colors",
            pinned
              ? "border-primary/40 text-primary"
              : "border-border text-muted-foreground hover:text-foreground",
          )}
        >
          {t("autoChip")}
        </button>
      </header>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4"
        data-slot="voice-transcript-scroll"
      >
        {captions.map((seg) => {
          const isPersona = seg.speaker === "persona";
          const asMarkdown = isPersona && seg.isFinal;
          return (
            <div
              key={seg.segmentId}
              className={cn(
                "flex items-start gap-2.5",
                !seg.isFinal && "opacity-80",
              )}
              data-slot="voice-transcript-turn"
              data-speaker={seg.speaker}
            >
              {isPersona ? (
                <PersonaAvatar persona={persona} size="sm" />
              ) : (
                <UserAvatar className="size-6" />
              )}
              <div className="min-w-0 flex-1 pt-0.5 text-sm leading-relaxed">
                {/* Speaker attribution stays available to assistive tech even
                    though the avatar carries it visually (R9-029). */}
                <span className="sr-only">
                  {isPersona ? persona.name : t("you")}:{" "}
                </span>
                {asMarkdown ? (
                  <Markdown>{seg.text}</Markdown>
                ) : (
                  <span>{seg.text}</span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
