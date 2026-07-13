"use client";

import { ArrowUp } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { useMeEvent } from "@/components/providers/me-events-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { buttonVariants } from "@/components/ui/button";
import { DocumentChip } from "@/components/ui/document-chip";
import { Textarea } from "@/components/ui/textarea";
import type { ApiError } from "@/lib/api/client";
import {
  isLastAssistantMessage,
  isLastUserMessage,
} from "@/lib/chat/tail-target";
import { removeDocument } from "@/lib/document-actions";
import { useChat } from "@/lib/hooks/use-chat";
import { notifyConversationFilesChanged } from "@/lib/hooks/use-conversation-artifacts";
import { useConversationDocuments } from "@/lib/hooks/use-conversation-documents";
import type { TurnIntoFileFormat } from "@/lib/turn-into-file";
import { turnMessageIntoFile } from "@/lib/turn-into-file";
import type { DocumentRef } from "@/lib/upload";
import { cn } from "@/lib/utils";
import { CHAT_STREAMING_EVENT } from "./chat-presence-orb";
import { ComposerAttachControl } from "./composer/attach-control";
import {
  attachmentsBlockSend,
  type ValidationReason,
  validateBeforeUpload,
} from "./composer/attach-state";
import { ComposerImageChip } from "./composer/image-preview";
import { NoVisionErrorBanner } from "./composer/no-vision-error-banner";
import { useDragTarget, usePasteImage } from "./composer/use-attach-non-click";
import { useComposerAttachments } from "./composer/use-composer-attachments";
import { surfaceValidationFailure } from "./composer/validation-toast";
import { FileRendererProvider } from "./file-renderer-context";
import { FileRendererPanel } from "./file-renderer-panel";
import { type ChatMessageView, MessageElement } from "./message-element";
import { MicDictation } from "./mic-dictation";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

// Spec 35 — "stick to bottom" follow logic. The reader counts as pinned to the
// latest when within this many px of the bottom; beyond it, streaming chunks
// stop auto-scrolling so they can read earlier turns undisturbed.
const PIN_THRESHOLD_PX = 80;

/**
 * F3 (T19) — strangler-fig composer wiring.
 *
 * Extends F2's ChatWindow with the file-attach surface: attach control +
 * image preview tray + conversation document panel + at-send no-vision
 * safety banner. The existing send/streaming/auto-scroll plumbing
 * (`useChat`, `MessageElement` per-message render, the textarea form)
 * is UNCHANGED — F3 ADDS adjacent surfaces, not refactors.
 *
 * **Decision touchpoints honoured:**
 *   - D-F3-X-capabilities-prop-drill-shape: `capabilities` arrives as a
 *     plain prop from the page (no context, no global). Prop chain is
 *     PersonaDetail → ChatPage → ChatWindow → ComposerAttachControl.
 *   - D-F3-X-document-attach-conversation-binding: `documentsDisabled` is
 *     always `false` here because ChatWindow is mounted only inside a
 *     conversation context. The persona-detail surface (which lacks a
 *     conversationId) never mounts ChatWindow.
 *   - D-F3-X-cap-attached-state-on-conversation-switch: enforced inside
 *     `useComposerAttachments` (sole-dep `[conversationId]` reset).
 *   - D-F3-X-partial-upload-failure-shape: `attachmentsBlockSend` predicate
 *     disables the send button while any image is uploading/error.
 *   - D-F3-X-no-vision-surface-shape: (a) attach disabled via
 *     `imageAttachDisabled`; (c) at-send `<NoVisionErrorBanner>` surfaces
 *     the runtime refusal.
 */
export interface PersonaCapabilities {
  vision: boolean;
  configured_tiers: readonly string[];
}

export function ChatWindow({
  conversationId,
  persona,
  initialMessages,
  capabilities,
  personaLanguage,
}: {
  conversationId: string;
  persona: AvatarPersona;
  initialMessages: ChatMessageView[];
  /** Deployment capabilities surfaced by Spec 08 PersonaDetail (T02). */
  capabilities?: PersonaCapabilities | null;
  /**
   * The conversation persona's declared `identity.language_default`
   * (R9-025 reopen — context-pinned dictation), already parsed server-side
   * by the page from the SAME persona YAML `capabilities` comes from — no
   * extra fetch. Threaded to `<MicDictation>` so in-chat dictation pins to
   * the persona's language instead of Deepgram's `detect_language`, which
   * mishandles Norwegian (mirrors the live call pipeline's per-persona
   * pin, Spec 32). `null`/omitted → the mic falls back to auto-detect.
   */
  personaLanguage?: string | null;
}) {
  const t = useTranslations("chat");
  // Every chat notification routes through useNotify (the single façade, D-35-10):
  // consequential events (a document landed / failed) persist in the bell;
  // transient validation passes persist:false so it toasts without the bell.
  const { notify } = useNotify();
  const { getToken } = useAuth();
  const {
    messages,
    streaming,
    error,
    send,
    respondToProactive,
    reload,
    regenerate,
    editAndRerun,
  } = useChat(conversationId, initialMessages, persona.id);
  // R9-025 leg C — retry: a REAL server-side regenerate (supersedes the old
  // reply, re-runs on the SAME preceding user turn) — see the retry-seam
  // decision note in message-element.tsx and use-chat.ts's `regenerate`.
  const handleRetryMessage = useCallback(
    (assistantMessageId: string) => {
      void regenerate(assistantMessageId);
    },
    [regenerate],
  );
  // R9-025 leg C — edit: save-and-rerun the LAST user message's content — see
  // use-chat.ts's `editAndRerun`.
  const handleEditMessage = useCallback(
    (userMessageId: string, newContent: string) => {
      void editAndRerun(userMessageId, newContent);
    },
    [editAndRerun],
  );
  // R9-025b — "Turn into file": fire-and-forget the durable extraction job.
  // Optimistic toast (transient — the job runs out of band; the Files panel
  // picks the artifact up on its own refresh/poll, see the api's
  // `file_extract` module docstring for the exact refresh-signal decision).
  const handleTurnIntoFile = useCallback(
    (assistantMessageId: string, format: TurnIntoFileFormat) => {
      notify({
        level: "info",
        title: t("actions.turnIntoFile.toast"),
        persist: false,
      });
      void turnMessageIntoFile(conversationId, assistantMessageId, format, () =>
        getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
      ).catch(() => {
        notify({
          level: "error",
          title: t("actions.turnIntoFile.errorToast"),
          persist: false,
        });
      });
    },
    [conversationId, getToken, notify, t],
  );
  // Spec A11: a BACKGROUND message.delivered for THIS open conversation appears live —
  // refetch the authoritative conversation (never trust the pushed payload; `reload`
  // reconciles by message id, so an already-rendered message is not doubled — A11-D-4).
  // Skip while a turn is streaming: the in-turn active-run stream is authoritative for
  // its own turn; the background message reconciles on the next natural reload/reattach.
  // A `resync` (the channel couldn't honour our resume cursor) also refetches.
  const onMessageDelivered = useCallback(
    (data: Record<string, unknown>) => {
      if (!streaming && data.conversation_id === conversationId) void reload();
    },
    [streaming, conversationId, reload],
  );
  const onResync = useCallback(() => {
    if (!streaming) void reload();
  }, [streaming, reload]);
  useMeEvent("message.delivered", onMessageDelivered);
  useMeEvent("resync", onResync);
  const [input, setInput] = useState("");
  const [sendError, setSendError] = useState<ApiError | null>(null);
  // Spec 35: documents attached for the NEXT message (so they ride that turn's
  // bubble as chips). Distinct from `documents` (the conversation's full doc
  // context the persona reads). Reset on conversation switch.
  const [pendingDocs, setPendingDocs] = useState<DocumentRef[]>([]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset on conversation switch only
  useEffect(() => {
    setPendingDocs([]);
  }, [conversationId]);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const dropZoneRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // True while the reader is near the bottom and should follow new content.
  // Flipped false the moment they scroll up; re-armed when they return or send.
  const pinnedRef = useRef(true);

  // Conversation-scoped document state (separate slice from message-scoped
  // image attachments — D-F3-X-cap-attached-state-on-conversation-switch
  // explicitly notes the two lifecycles are distinct).
  const documents = useConversationDocuments(conversationId);

  // Image-attachment state + upload orchestration (T13 hook).
  // F3 follow-up — surface a success toast on document attach so the user
  // sees confirmation of "{filename} added to this conversation" (the chip
  // appearing in the panel is the persistent signal, but the toast makes
  // the per-message action visible — addresses the "did it do anything?"
  // ambiguity surfaced during Phase 6 operator testing).
  const attach = useComposerAttachments({
    conversationId,
    personaId: persona.id,
    onDocumentAttached: (ref) => {
      documents.addOptimistic(ref);
      // Carry it on the next message's bubble + keep the Files viewer in sync.
      setPendingDocs((prev) =>
        prev.some((d) => d.doc_ref === ref.doc_ref) ? prev : [...prev, ref],
      );
      notifyConversationFilesChanged();
      notify({
        level: "success",
        title: t("composer.attach.feedback.documentAttached", {
          filename: ref.filename,
        }),
      });
    },
    onDocumentError: (detail) => notify({ level: "error", title: detail }),
  });

  // D-F3-X-no-vision-surface-shape (a): attach disabled when the
  // deployment has no vision tier configured. `null` (capabilities not
  // wired) leaves attach enabled — server stays authoritative per (c).
  const imageAttachDisabled = capabilities?.vision === false;

  const handleReject = useCallback(
    (reason: ValidationReason, detail: string) => {
      // Transient: routes through useNotify but stays out of the bell. The sink
      // shim adapts notify to the { error } shape the helper expects.
      surfaceValidationFailure(
        reason,
        detail,
        {
          error: (msg) =>
            notify({ level: "error", title: msg, persist: false }),
        },
        t,
      );
    },
    [notify, t],
  );

  // T08 — drag-and-drop + paste handlers (desktop-only enhancements).
  useDragTarget(dropZoneRef, {
    onFiles: (files) => {
      for (const f of files) {
        const result = validateBeforeUpload(f, attach.attachedImages.length);
        if (!result.ok) {
          handleReject(result.reason, result.detail);
          continue;
        }
        if (result.kind === "image") {
          if (imageAttachDisabled) {
            handleReject(
              "unsupported_format",
              t("composer.attach.imageDisabled"),
            );
            continue;
          }
          attach.attachImage(f);
        } else {
          void attach.uploadDocumentFile(f);
        }
      }
    },
    onReject: (detail) =>
      notify({ level: "error", title: detail, persist: false }),
  });
  usePasteImage(textareaRef, {
    onFile: (file) => {
      if (imageAttachDisabled) {
        handleReject("unsupported_format", t("composer.attach.imageDisabled"));
        return;
      }
      attach.attachImage(file);
    },
  });

  // Track whether the reader is pinned to the bottom. Cheap, ref-only (no
  // re-render on scroll); read by the follow effect below.
  const handleScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    pinnedRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < PIN_THRESHOLD_PX;
  }, []);

  // Follow the newest content ONLY while pinned — so streaming doesn't drag the
  // reader down while they scroll up to re-read (instant, not smooth, to avoid
  // self-fighting animations across rapid chunk updates).
  // biome-ignore lint/correctness/useExhaustiveDependencies: follow newest, pinned-only
  useEffect(() => {
    if (!pinnedRef.current) return;
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // Spec 35 D-35-7: broadcast the live/composing state so the chat-header
  // presence orb (rendered above this component, in the page) can pulse while
  // the persona is live — decoupled, no streaming prop-drilled up to the page.
  useEffect(() => {
    window.dispatchEvent(
      new CustomEvent(CHAT_STREAMING_EVENT, { detail: streaming }),
    );
  }, [streaming]);

  async function submit() {
    const value = input.trim();
    if (!value || streaming) return;
    if (attachmentsBlockSend(attach.attachedImages)) return;

    // Collect ImageRef[] from the success-state attachments. The mediaType
    // came from the upload-service response (Spec 13 always returns one of
    // the 4 supported MIME types per the route's content-type dispatch),
    // so the literal-union cast is safe.
    const refs = attach.attachedImages
      .filter((a) => a.state === "success")
      .map((a) => {
        const success = a as { workspacePath: string; mediaType: string };
        return {
          workspace_path: success.workspacePath,
          media_type: success.mediaType as
            | "image/png"
            | "image/jpeg"
            | "image/webp"
            | "image/gif",
        };
      });

    const docs = pendingDocs.map((d) => ({
      doc_ref: d.doc_ref,
      filename: d.filename,
      format: d.format,
      size_bytes: d.size_bytes ?? null,
      strategy: d.strategy as
        | "whole_inject"
        | "retrieval"
        | "vision_handoff"
        | undefined,
    }));

    setInput("");
    setSendError(null);
    // The reader's own send always returns them to the latest turn.
    pinnedRef.current = true;
    // Clear the composer NOW — the attachments are captured and ride the sent
    // message + persist in the Files viewer. `await send()` only resolves after
    // the whole reply streams, so clearing there would leave the chips lingering
    // through the response.
    attach.clearImages();
    setPendingDocs([]);
    try {
      await send(value, refs, docs);
    } catch (e) {
      // useChat already surfaces error state via its own setError; catch
      // ApiError specifically so the NoVision banner can pattern-match.
      if (e && typeof e === "object" && "status" in e) {
        setSendError(e as ApiError);
      }
    }
  }

  const sendBlocked =
    streaming || !input.trim() || attachmentsBlockSend(attach.attachedImages);

  const onDocumentRemove = useCallback(
    async (docRef: string) => {
      documents.removeOptimistic(docRef);
      setPendingDocs((prev) => prev.filter((d) => d.doc_ref !== docRef));
      try {
        await removeDocument(
          conversationId,
          docRef,
          async () =>
            (await getToken(TEMPLATE ? { template: TEMPLATE } : undefined)) ??
            "",
        );
      } catch {
        // On failure, refresh to reconcile with server state.
        void documents.refresh();
      }
    },
    [conversationId, documents, getToken],
  );

  return (
    <FileRendererProvider>
      <div className="flex min-h-0 flex-1 flex-col" ref={dropZoneRef}>
        {/* Spec 28 — conversation-scoped right-panel renderer (D-28-6). */}
        <FileRendererPanel personaId={persona.id} />
        <div
          ref={scrollerRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto"
        >
          <div className="mx-auto flex w-full max-w-2xl flex-col gap-5 px-4 py-6">
            {messages.length === 0 ? (
              <p className="py-10 text-center text-sm text-muted-foreground">
                {t("empty")}
              </p>
            ) : null}
            {messages.map((m, i) => {
              // R9-025 leg C — v1 scope: regenerate/edit render ONLY on the
              // conversation TAIL (`lib/chat/tail-target.ts`, mirroring the
              // api's `_tail_target` rule exactly).
              return (
                <MessageElement
                  key={m.id}
                  message={m}
                  persona={persona}
                  prevMessage={i > 0 ? messages[i - 1] : undefined}
                  onRespondToProactive={respondToProactive}
                  onRetryMessage={
                    isLastAssistantMessage(messages, i)
                      ? handleRetryMessage
                      : undefined
                  }
                  retryDisabled={streaming}
                  onEditMessage={
                    isLastUserMessage(messages, i)
                      ? handleEditMessage
                      : undefined
                  }
                  editDisabled={streaming}
                  onTurnIntoFile={handleTurnIntoFile}
                  turnIntoFileDisabled={streaming}
                />
              );
            })}
            {error ? (
              <p className="text-sm text-destructive">{t("error")}</p>
            ) : null}
          </div>
        </div>

        {/* F3: NoVision at-send safety banner (D-F3-X-no-vision-surface-shape (c)). */}
        <NoVisionErrorBanner
          error={sendError}
          onDismiss={() => {
            setSendError(null);
            attach.clearImages();
          }}
        />

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
          className="border-t bg-background/80 backdrop-blur"
        >
          <div className="mx-auto flex w-full max-w-2xl flex-col gap-2 px-4 py-3">
            {/* Spec 35: one compact plain-chip row for the message's pending
                attachments — images + documents alike (no big preview tray).
                The files also persist in the header Files viewer. */}
            {attach.attachedImages.length > 0 || pendingDocs.length > 0 ? (
              <div
                className="flex flex-wrap gap-2"
                data-slot="composer-attachments"
              >
                {attach.attachedImages.map((a) => (
                  <ComposerImageChip
                    key={a.id}
                    attachment={a}
                    onRemove={attach.removeImage}
                  />
                ))}
                {pendingDocs.map((d) => (
                  <DocumentChip
                    key={d.doc_ref}
                    docRef={d.doc_ref}
                    filename={d.filename}
                    format={d.format}
                    sizeBytes={d.size_bytes ?? null}
                    strategy={
                      d.strategy as
                        | "whole_inject"
                        | "retrieval"
                        | "vision_handoff"
                    }
                    onRemove={(docRef) => void onDocumentRemove(docRef)}
                  />
                ))}
              </div>
            ) : null}

            <div className="flex items-center gap-2">
              {/* F3 T07 — attach control. documentsDisabled is always false
                here (we're inside a conversation context per
                D-F3-X-document-attach-conversation-binding). */}
              <ComposerAttachControl
                onImageFile={attach.attachImage}
                onDocumentFile={(file) => void attach.uploadDocumentFile(file)}
                onReject={handleReject}
                currentImageCount={attach.attachedImages.length}
                imageAttachDisabled={imageAttachDisabled}
                documentsDisabled={false}
              />
              {/* R9-025a — in-chat mic dictation: record -> transcribe ->
                  insert the editable transcript at the caret. Hidden when
                  the voice feature is absent (fail-soft). R9-025 reopen:
                  pinned to the persona's declared language so Deepgram's
                  detect_language isn't relied on for non-English speech. */}
              <MicDictation
                value={input}
                onChange={setInput}
                textareaRef={textareaRef}
                disabled={streaming}
                language={personaLanguage ?? undefined}
              />
              <Textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void submit();
                  }
                }}
                placeholder={t("placeholder", { name: persona.name })}
                rows={1}
                className="max-h-40 min-h-10 flex-1 resize-none field-sizing-content"
              />
              <button
                type="submit"
                disabled={sendBlocked}
                aria-label={t("send")}
                className={cn(buttonVariants({ size: "icon" }))}
              >
                <ArrowUp className="size-4" />
              </button>
            </div>

            {/* Spec 35: the editorial composer hint — the send-key affordance,
                mono-muted (mirrors .v-composer__hint). R9-026: the "Shared
                memory with voice · constraints enforced" caption that used to
                share this row is removed entirely (dropped, not hidden). */}
            <div className="flex items-center justify-end gap-3 px-1 type-caption normal-case tracking-normal text-muted-foreground">
              <span className="hidden shrink-0 sm:inline">{t("hintKeys")}</span>
            </div>
          </div>
        </form>
      </div>
    </FileRendererProvider>
  );
}
