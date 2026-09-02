"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useMemo, useState } from "react";

/**
 * Spec F2 T15 + T26 — MessageElement.
 *
 * Replaces the scaffold's <MessageBubble> as the F2 canonical chat-message
 * presentation. Composes T13/T17/T16/T11/T06.
 *
 * D-F1-5 composite (locked from F1 closeout + decisions.md):
 *   - identity-coloured <PersonaAvatar> at top of each persona TURN
 *     (D-F2-7 once-per-turn rule — consecutive persona messages share one
 *     avatar; broken by a user message → render again);
 *   - 1px identity-coloured underline beneath the persona name (lives in
 *     <PersonaIdentityHeader>, used at the chat top, not per-message);
 *   - 2px identity-coloured `border-left` on each persona message wrapper.
 *
 * **D-F2-15 (NEW, post-T22 user-driven amendment):** interleaved tool layout.
 * The scaffold's MessageBubble (and the original T15 stacked layout) showed
 * all tool-call cards above the text content, batched together regardless
 * of when they happened in the stream. The user-reported chat-UX issue
 * (2026-06-06) was: (1) no thinking indicator before first chunk; (2) no
 * tool-running indicator during the pause between text spans while tools
 * execute; (3) tool cards clumping at the top; (4) text concatenation
 * losing the gap where tools happened. D-F2-15 introduces an event-log
 * rendering mode that walks `message.events[]` in stream order and emits
 * text spans + tool cards inline at the position they arrived. Falls back
 * to the stacked layout when `events[]` is absent (back-compat for tests
 * + the older API surface).
 *
 * Activity indicator state machine (D-F2-13 + the new tool-running state):
 *   - streaming && events is empty           → ThinkingIndicator (3 muted dots)
 *   - streaming && last event is text        → vermilion Caret on the last span
 *   - streaming && a tool is pending         → ToolRunningIndicator (name pulse)
 *   - !streaming                             → no indicator
 *
 * User branch: right-aligned, `rounded-2xl rounded-br-sm`, `bg-secondary`,
 * `.type-body`. The `max-w-[80%]` is a percentage layout (positional, not
 * a design value; audit.md §grep-gate-seed).
 *
 * Tier badge sits below the body when the message is terminal.
 */

import { ActivityState } from "@/components/activity-state";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { AskUserPrompt } from "@/components/runs/ask-user-prompt";
import { buttonVariants } from "@/components/ui/button";
import { DocumentChip } from "@/components/ui/document-chip";
import { Markdown } from "@/components/ui/markdown";
import { Textarea } from "@/components/ui/textarea";
import type { ActivityView } from "@/lib/activity";
import type { OutputContent } from "@/lib/api/output-content";
import { operationFor, projectToolResult } from "@/lib/normalisers/_classify";
import { personaIdentityStyle } from "@/lib/persona-identity";
import type {
  ArtifactRef,
  BudgetSnapshot,
  ProactiveProposal,
  ProducedFileRef,
  QuestionOption,
  RoutingSummary,
} from "@/lib/sse-types";
import type { TurnIntoFileFormat } from "@/lib/turn-into-file";
import { cn } from "@/lib/utils";
import { AuthedImage } from "./authed-image";
import { BudgetIndicator } from "./budget-indicator";
import { MessageActionBar } from "./message-action-bar";
import { OutputDispatcher } from "./output/dispatcher";
import { ImageLightbox } from "./output/image-lightbox";
import { StreamingTextRenderer } from "./streaming-text-renderer";
import { TierBadge } from "./tier-badge";
import { ToolCallCard, type ToolEntry } from "./tool-call-card";
import { UserAvatar } from "./user-avatar";

/**
 * Ordered event in the assistant stream. Drives the D-F2-15 interleaved
 * rendering: tool_call events emit inline tool cards at their actual stream
 * position; text events accumulate into spans before / between / after them.
 */
export type MessageEvent =
  | { kind: "text"; delta: string }
  | {
      kind: "tool_call";
      callId: string;
      toolName: string;
      args?: Record<string, unknown>;
      /** Spec 30 T01 (D-30-1): source badge — builtin/skill/mcp:builtin/mcp:optional. */
      toolKind?: string;
    }
  | {
      kind: "tool_result";
      toolName: string;
      content: string;
      isError: boolean;
      /** Spec 30 T01 (D-30-1): source badge (mirrors the tool_call arm). */
      toolKind?: string;
      /**
       * F4 T02b + T10 (D-F4-X-event-kind-for-produced-files): structured
       * produced_files surfaced from the runtime via use-chat. Optional —
       * absent on pre-amendment frames and on tools that don't produce
       * files (web_search, file_*). Consumed by the F4 OutputDispatcher
       * in InterleavedContent below.
       */
      producedFiles?: ProducedFileRef[];
      /**
       * Spec 28: persisted artifacts (image / file / diagram). Preferred over
       * producedFiles by the OutputDispatcher (the unified FileCard path).
       */
      artifacts?: ArtifactRef[];
    };

/**
 * The message shape <MessageElement> consumes. `events[]` is the
 * D-F2-15 ordered log; `content` + `tools[]` are kept as derived/legacy
 * fields so older consumers continue to work.
 *
 * F3 (T06) — `images?: ImageRef[]` is optional and additive. When present
 * on a user-role message, MessageElement (T10) renders the attached
 * images inline above the text via `<AuthedImage>` (Bearer-fetched blob
 * URLs per D-F3-X-image-serve-auth). Absent → text-only path unchanged.
 */
export interface MessageElementView {
  id: string;
  role: string;
  content: string;
  tier?: string;
  /** Spec 31 (D-31-1/2): the model decision + budget for this turn, when intelligent routing ran. */
  routing?: RoutingSummary;
  budget?: BudgetSnapshot;
  tools?: ToolEntry[];
  /** D-F2-15: ordered event log. When present, MessageElement renders interleaved. */
  events?: MessageEvent[];
  streaming?: boolean;
  /**
   * The model is generating the current round but hasn't emitted text or a tool
   * call yet — driven by the `thinking` SSE event. Shows a "working" pulse in
   * the gap (notably while writing a long code_execution call, whose args don't
   * stream as deltas). Cleared on the round's first chunk / tool event.
   */
  working?: boolean;
  /** F3 (T06): workspace references to attached images on this message. */
  images?: { workspace_path: string; media_type: string }[];
  /**
   * Spec 35: documents attached to THIS turn — rendered as plain chips in the
   * user bubble (images render inline; files don't preview). Carried optimistically
   * at send so the file is visible in the chat, not only in the Files viewer.
   */
  documents?: {
    doc_ref: string;
    filename: string;
    format: string;
    size_bytes: number | null;
    strategy?: "whole_inject" | "retrieval" | "vision_handoff";
  }[];
  /**
   * Spec 30 (D-30-2): an in-chat proactive question (the chat rail). Present on
   * an assistant turn that ended with a tool-gap / MCP-gap consent offer (or a
   * Spec-21 clarifying ask). Rendered inline via the shared 3+1 prompt; the
   * `proposal` (when present) drives accept → grant → retry.
   */
  proactive?: {
    question: string;
    options?: QuestionOption[];
    allowFreeForm?: boolean;
    proposal?: ProactiveProposal;
  };
  /**
   * Spec 35 (D-35-4): the typed-memory recall trace for this turn — one entry
   * per store consulted while composing, in order. Drives the staged
   * "Recalling from <store> memory" state on the streaming assistant turn (the
   * store-coloured pulse). Absent on historical / non-streaming turns.
   */
  recall?: { store: string; count?: number }[];
  /**
   * P2 — the live "using <X>…" activity states for this turn (a SEPARATE channel from
   * {@link tools}: `activity_start` opens an entry, `activity_end` resolves it by
   * `activityId`). Kept apart from `tools` so a call renders ONE tool card plus a
   * transient activity state — never two cards (P2-D-3). Absent on historical /
   * non-streaming turns.
   */
  activities?: ActivityView[];
}

/**
 * Canonical name used by useChat + chat-window. Aliased so the old import
 * path (`./message-bubble`) can be removed without touching every consumer.
 */
export type ChatMessageView = MessageElementView;

interface MessageElementProps {
  message: MessageElementView;
  /**
   * The persona this conversation is with. Drives the identity colour for
   * the D-F1-5 composite. F2 ships single-persona conversations; future
   * multi-persona work would key the once-per-turn rule on persona id too.
   */
  persona: AvatarPersona;
  /**
   * The immediately-preceding message in the conversation, if any. Drives
   * the D-F2-7 once-per-turn avatar rule.
   */
  prevMessage?: MessageElementView;
  className?: string;
  /**
   * Spec 30 (D-30-2): answer an in-chat proactive question. `isAccept` is true
   * when the user picked the enable option of a capability-gap offer; the
   * `proposal` (when present) drives accept → grant → retry in useChat.
   */
  onRespondToProactive?: (
    messageId: string,
    answer: string,
    opts: { isAccept: boolean; proposal?: ProactiveProposal },
  ) => Promise<void>;
  /**
   * R9-025 leg C — regenerate: a REAL server-side retry of the tail assistant
   * reply (see use-chat.ts's `regenerate`). Omit to hide the retry button
   * entirely (e.g. a read-only transcript viewer, or — per the v1
   * conversation-TAIL-only scope — this isn't the last assistant message; the
   * caller, chat-window.tsx, only wires this for the tail).
   */
  onRetryMessage?: (assistantMessageId: string) => void;
  /** Disable retry while a turn is active (mirrors the composer's own `sendBlocked`). */
  retryDisabled?: boolean;
  /**
   * R9-025 leg C — edit: save-and-rerun the LAST user message's content (see
   * use-chat.ts's `editAndRerun`). Omit to hide the edit button entirely
   * (same v1 tail-only gating as `onRetryMessage`, mirrored for the user
   * side — the caller only wires this for the last user message).
   */
  onEditMessage?: (userMessageId: string, newContent: string) => void;
  /** Mirrors `retryDisabled` — disabled while ANY turn is active. */
  editDisabled?: boolean;
  /**
   * R9-025b — "Turn into file": fires the extraction job for this assistant
   * message at the chosen format. Omit to hide the action entirely (e.g. a
   * read-only transcript viewer, or the feature not wired).
   */
  onTurnIntoFile?: (
    assistantMessageId: string,
    format: TurnIntoFileFormat,
  ) => void;
  /** R9-025b — disable while a turn is active (mirrors `retryDisabled`, the same 2a `streaming` guard). */
  turnIntoFileDisabled?: boolean;
}

export function MessageElement({
  message,
  persona,
  prevMessage,
  className,
  onRespondToProactive,
  onRetryMessage,
  retryDisabled,
  onEditMessage,
  editDisabled,
  onTurnIntoFile,
  turnIntoFileDisabled,
}: MessageElementProps) {
  if (message.role === "user") {
    return (
      <UserMessage
        message={message}
        persona={persona}
        className={className}
        onEditMessage={onEditMessage}
        editDisabled={editDisabled}
      />
    );
  }
  return (
    <PersonaMessage
      message={message}
      persona={persona}
      prevMessage={prevMessage}
      className={className}
      onRespondToProactive={onRespondToProactive}
      onRetryMessage={onRetryMessage}
      retryDisabled={retryDisabled}
      onTurnIntoFile={onTurnIntoFile}
      turnIntoFileDisabled={turnIntoFileDisabled}
    />
  );
}

function UserMessage({
  message,
  persona,
  className,
  onEditMessage,
  editDisabled,
}: {
  message: MessageElementView;
  persona: AvatarPersona;
  className?: string;
  onEditMessage?: (userMessageId: string, newContent: string) => void;
  editDisabled?: boolean;
}) {
  // F3 (T10) — attached images render INSIDE the bubble, above the text,
  // via AuthedImage (Bearer-fetched blob URLs per D-F3-X-image-serve-auth).
  // Empty/absent `images` → text-only path renders byte-for-byte unchanged.
  const hasImages = message.images && message.images.length > 0;
  const hasDocuments = message.documents && message.documents.length > 0;

  // R9-025 leg C — inline edit: local draft-text state owned HERE (not the
  // action bar, which is a stateless control). `editing` swaps the bubble for
  // a form; the pencil button (wired only when `onEditMessage` is present —
  // chat-window.tsx only ever wires it for the LAST user message, the v1
  // tail-only scope) opens it.
  const t = useTranslations("chat");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);

  const startEdit = useCallback(() => {
    setDraft(message.content);
    setEditing(true);
  }, [message.content]);

  const cancelEdit = useCallback(() => setEditing(false), []);

  const saveEdit = useCallback(() => {
    const trimmed = draft.trim();
    if (!trimmed || !onEditMessage) return;
    setEditing(false);
    onEditMessage(message.id, trimmed);
  }, [draft, onEditMessage, message.id]);

  return (
    <div
      className={cn("group flex items-start justify-end gap-2.5", className)}
      data-slot="message-element"
      data-role="user"
    >
      {/* R9-025a: the bubble + hover/focus action bar share a column so the
          bar sits BELOW the bubble, right-aligned to match it. The bubble
          itself keeps `data-slot="message-bubble"` + every existing class —
          tests target it by that slot (not `firstElementChild`) precisely
          so this wrapper can exist without becoming a fragile coupling. */}
      <div className="flex max-w-[80%] flex-col items-end gap-1">
        {editing ? (
          <div
            data-slot="message-edit-form"
            className="type-body flex w-full flex-col gap-2 rounded-2xl bg-secondary px-3 py-2.5 text-secondary-foreground"
          >
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={!!editDisabled}
              autoFocus
              rows={3}
              aria-label={t("actions.edit")}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  saveEdit();
                } else if (e.key === "Escape") {
                  cancelEdit();
                }
              }}
              className="bg-background"
            />
            <div className="flex justify-end gap-1.5">
              <button
                type="button"
                onClick={cancelEdit}
                data-slot="message-edit-cancel"
                className={buttonVariants({ variant: "ghost", size: "sm" })}
              >
                {t("actions.editCancel")}
              </button>
              <button
                type="button"
                onClick={saveEdit}
                disabled={!draft.trim() || !!editDisabled}
                data-slot="message-edit-save"
                className={buttonVariants({ variant: "default", size: "sm" })}
              >
                {t("actions.editSave")}
              </button>
            </div>
          </div>
        ) : (
          <>
            <div
              data-slot="message-bubble"
              className="type-body flex w-full flex-col gap-2 rounded-2xl rounded-br-sm bg-secondary px-4 py-2.5 text-secondary-foreground"
            >
              {hasImages ? (
                <div
                  className="flex flex-wrap gap-2"
                  data-slot="message-images"
                  data-count={message.images?.length}
                >
                  {message.images?.map((img) => (
                    <AuthedImage
                      key={img.workspace_path}
                      personaId={persona.id}
                      workspacePath={img.workspace_path}
                      mediaType={img.media_type}
                      alt={
                        img.workspace_path.split("/").pop() ??
                        t("attachedImageAlt")
                      }
                    />
                  ))}
                </div>
              ) : null}
              {/* Spec 35: attached documents render as read-only plain chips
                  (no preview) so the user sees the file they sent in the
                  thread. */}
              {hasDocuments ? (
                <div
                  className="flex flex-wrap gap-2"
                  data-slot="message-documents"
                  data-count={message.documents?.length}
                >
                  {message.documents?.map((d) => (
                    <DocumentChip
                      key={d.doc_ref}
                      docRef={d.doc_ref}
                      filename={d.filename}
                      format={d.format}
                      sizeBytes={d.size_bytes}
                      strategy={d.strategy}
                    />
                  ))}
                </div>
              ) : null}
              {message.content ? (
                <span className="whitespace-pre-wrap">{message.content}</span>
              ) : null}
            </div>
            <MessageActionBar
              messageRole="user"
              content={message.content}
              onEdit={onEditMessage ? startEdit : undefined}
              editDisabled={editDisabled}
            />
          </>
        )}
      </div>
      {/* Spec 35: the user's own profile avatar on their turns — mirrors the
          persona avatar on the other side of the thread. */}
      <UserAvatar className="shrink-0" />
    </div>
  );
}

/**
 * R9-025 leg C retry-seam decision (supersedes the R9-025a note): the operator
 * pass on the wave-2a client-side echo-resend ("re-send the preceding user
 * message as a brand-new turn") found the model saw its OWN just-superseded
 * reply still sitting in context and produced a confused answer. Retry now
 * calls `POST …/messages/{id}/regenerate` (`useChat.regenerate`) — a REAL
 * server-side turn that supersedes the old reply (excluded from BOTH the next
 * model prompt and the listing) before re-running. `regenerate` mirrors
 * `send()`'s own concurrency posture (self-blocks while streaming, reattaches
 * on a raced 409), so retry still never bypasses the one-active-turn
 * invariant; `retryDisabled` (mirrors the composer's `sendBlocked`) is the
 * belt-and-braces UI-level guard. `onRetryMessage` is wired ONLY on the LAST
 * assistant message (chat-window.tsx) — the v1 conversation-TAIL-only scope.
 */
function PersonaMessage({
  message,
  persona,
  prevMessage,
  className,
  onRespondToProactive,
  onRetryMessage,
  retryDisabled,
  onTurnIntoFile,
  turnIntoFileDisabled,
}: {
  message: MessageElementView;
  persona: AvatarPersona;
  prevMessage?: MessageElementView;
  className?: string;
  onRespondToProactive?: (
    messageId: string,
    answer: string,
    opts: { isAccept: boolean; proposal?: ProactiveProposal },
  ) => Promise<void>;
  onRetryMessage?: (assistantMessageId: string) => void;
  retryDisabled?: boolean;
  onTurnIntoFile?: (
    assistantMessageId: string,
    format: TurnIntoFileFormat,
  ) => void;
  turnIntoFileDisabled?: boolean;
}) {
  // D-F2-7 once-per-turn rule: render the avatar UNLESS the previous message
  // was also a persona message (then this is a continuation of the same
  // turn and the avatar would be redundant). The 2px border-left already
  // marks every persona message; the avatar anchors identity at turn start.
  const showAvatar = !prevMessage || prevMessage.role === "user";

  const t = useTranslations("chat");
  const thinkingLabel = t("thinking", { name: persona.name });

  const hasEvents = message.events && message.events.length > 0;

  // F4 T12: per-message lightbox state. Tracks the workspace_path the
  // user clicked "view larger" on; closes via ESC / backdrop / close
  // button (D-F4-2 lightbox affordances).
  const [lightboxPath, setLightboxPath] = useState<string | null>(null);

  return (
    <div
      style={personaIdentityStyle(persona)}
      className={cn("v-msg__persona group", className)}
      data-slot="message-element"
      data-role="persona"
      data-shows-avatar={showAvatar ? "true" : "false"}
      data-layout={hasEvents ? "interleaved" : "stacked"}
    >
      <div className="size-10 shrink-0">
        {showAvatar ? <PersonaAvatar persona={persona} size="md" /> : null}
      </div>

      <div className="v-msg__body" data-slot="message-element-body">
        {/* Byline — the persona name (identity-underlined) above its card, the
            editorial turn-start anchor (Spec 35). Shown once per turn. */}
        {showAvatar ? (
          <div className="v-msg__byline">
            <span className="v-msg__byname">
              <span className="v-id-underline">{persona.name}</span>
            </span>
          </div>
        ) : null}

        {/* The message card — D-F1-5: 2px identity-coloured border-left (via
            --v-id) on the neutral card surface, now the editorial .v-msg__card. */}
        <div className="v-msg__card">
          {hasEvents ? (
            <InterleavedContent
              events={message.events ?? []}
              streaming={!!message.streaming}
              working={!!message.working}
              thinkingLabel={thinkingLabel}
              personaName={persona.name}
              personaId={persona.id}
              onViewLarger={setLightboxPath}
            />
          ) : message.streaming && !message.content ? (
            // Streaming, no content yet. Spec 35 (D-35-4): if the runtime has
            // named the typed-memory stores it's recalling, show that "thinking
            // / remembering" state (store-coloured); otherwise generic thinking.
            message.recall && message.recall.length > 0 ? (
              <RecallState recall={message.recall} />
            ) : (
              <StreamingTextRenderer
                text=""
                streaming
                thinking
                thinkingLabel={thinkingLabel}
              />
            )
          ) : (
            // Legacy stacked layout: tools above content (back-compat).
            <StackedContent message={message} thinkingLabel={thinkingLabel} />
          )}
          {/* P2 (T5): the live "using <X>…" state for an in-flight capability this
              turn — generalises the Spec 35 recall moment to every tool/skill/MCP.
              Renders only while streaming + an activity is in-flight (the component
              collapses to the current one and clears on resolve). */}
          {message.streaming ? (
            <ActivityState activities={message.activities} />
          ) : null}
        </div>

        {/* R9-025a: copy / retry / read-aloud — a terminal-only turn action
            (mid-stream content isn't final yet; retry mid-stream would race
            the in-progress turn rather than respect it). */}
        {!message.streaming ? (
          <MessageActionBar
            messageRole="persona"
            content={message.content}
            personaId={persona.id}
            onRetry={
              onRetryMessage ? () => onRetryMessage(message.id) : undefined
            }
            retryDisabled={retryDisabled}
            onTurnIntoFile={
              onTurnIntoFile
                ? (format) => onTurnIntoFile(message.id, format)
                : undefined
            }
            turnIntoFileDisabled={turnIntoFileDisabled}
          />
        ) : null}

        {message.tier && !message.streaming ? (
          <div className="v-msg__foot" data-slot="turn-transparency">
            <TierBadge tier={message.tier} routing={message.routing} />
            <BudgetIndicator budget={message.budget} />
          </div>
        ) : null}

        {/* Spec 30 (D-30-2): the in-chat proactive-question rail. Renders the
            shared 3+1 prompt (reused from the run viewer — no second renderer)
            for a tool-gap / MCP-gap consent offer once the turn settles. The
            enable option grants + retries; other answers continue the chat. */}
        {message.proactive && !message.streaming && onRespondToProactive ? (
          <AskUserPrompt
            question={message.proactive.question}
            options={message.proactive.options}
            allowFreeForm={message.proactive.allowFreeForm}
            onAnswer={(answer) => {
              const proactive = message.proactive;
              const enableLabel = proactive?.options?.[0]?.label;
              const isAccept =
                !!proactive?.proposal &&
                !!enableLabel &&
                answer === enableLabel;
              return onRespondToProactive(message.id, answer, {
                isAccept,
                proposal: proactive?.proposal,
              });
            }}
          />
        ) : null}
      </div>

      {/* F4 T12: lightbox renders at message level so it portals out of
          the scroller. media_type is unknown here (the OutputContent has
          it but the workspace_path is the only id flowing through) —
          AuthedImage handles bytes via the GET surface regardless. */}
      {lightboxPath !== null ? (
        <ImageLightbox
          open={true}
          personaId={persona.id}
          workspacePath={lightboxPath}
          mediaType="image/png"
          alt={lightboxPath.split("/").pop() ?? "image"}
          onClose={() => setLightboxPath(null)}
        />
      ) : null}
    </div>
  );
}

/**
 * Spec 35 (D-35-4) — the live "thinking / remembering" state. While the persona
 * composes, the runtime emits a `memory_recall` frame per typed store consulted
 * (cluster C); this names the store currently being recalled with its F1
 * store-colour pulse. The `.v-recall-dot[data-store]` rule colours the dot from
 * the store token; the line reads "Recalling from <store> memory".
 */
function RecallState({
  recall,
}: {
  recall: { store: string; count?: number }[];
}) {
  const t = useTranslations("chat");
  const current = recall[recall.length - 1];
  return (
    <div className="v-think__line" data-slot="recall-state">
      <span
        className="v-recall-dot"
        data-store={current.store}
        aria-hidden="true"
      />
      <span className="type-caption font-mono normal-case text-muted-foreground">
        {t("recalling", { store: current.store })}
      </span>
    </div>
  );
}

/**
 * Legacy stacked layout (back-compat for messages WITHOUT an events[] array).
 * Tool cards stack above text; streaming text uses T17 mechanism B; terminal
 * uses T11 Markdown.
 */
function StackedContent({
  message,
  thinkingLabel,
}: {
  message: MessageElementView;
  thinkingLabel: string;
}) {
  return (
    <>
      {message.tools && message.tools.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          {message.tools.map((tool, i) => (
            <ToolCallCard key={`${tool.toolName}-${i}`} entry={tool} />
          ))}
        </div>
      ) : null}

      {message.streaming ? (
        <StreamingTextRenderer
          text={message.content}
          streaming
          thinking={!message.content}
          thinkingLabel={thinkingLabel}
        />
      ) : message.content ? (
        <div className="type-body" data-slot="message-element-content">
          <Markdown>{message.content}</Markdown>
        </div>
      ) : null}
    </>
  );
}

/**
 * D-F2-15 interleaved rendering. Walks events in stream order, emitting:
 *   - text spans (one per contiguous text-event run, with optional caret
 *     at the end if streaming and the last event was text);
 *   - inline ToolCallCard at the position each tool_call happened (matched
 *     with its tool_result by FIFO toolName matching — the chat-SSE
 *     tool_result frame doesn't carry a call_id, only the run-stream does).
 *
 * Pre-first-event thinking indicator + per-tool running indicator handle
 * the activity-state transitions.
 */
function InterleavedContent({
  events,
  streaming,
  working = false,
  thinkingLabel,
  personaName,
  personaId,
  onViewLarger,
}: {
  events: readonly MessageEvent[];
  streaming: boolean;
  /** Model is generating this round (thinking event); shows a working pulse. */
  working?: boolean;
  thinkingLabel: string;
  personaName: string;
  /** F4 T10: passed through to OutputDispatcher for Bearer-auth byte loading. */
  personaId: string;
  /** F4 T12: view-larger click handler — opens the message-level lightbox. */
  onViewLarger?: (workspacePath: string) => void;
}) {
  const t = useTranslations("chat");

  // Render items computed in a single pass via memo for stability across
  // re-renders that don't change events.
  const { items, pendingToolName, isLiveText } = useMemo(() => {
    const out: ReactNode[] = [];
    let textBuffer = "";
    let textIdx = 0;
    const consumedResults = new Set<number>();
    let pending: string | null = null;
    let lastKind: MessageEvent["kind"] | null = null;

    /**
     * Flush the buffered text run as a `<Markdown>` block, optionally
     * followed by the vermilion streaming caret.
     *
     * User feedback 2026-06-06 (two iterations):
     *  · "raw markdown not rendering" → switched plain-text spans to `<Markdown>`.
     *  · "we need to try render real time markdown … each token or chunks
     *     received" → run the FULL buffer through Markdown on every flush,
     *     not just up to the last `\n`. Inline `**bold**` / `` `code` ``
     *     settle as soon as both delimiters land instead of waiting for
     *     a paragraph break. The user accepted the brief raw-syntax flicker
     *     on incomplete inline pairs as the explicit trade-off.
     *
     * Caret placement: when `showCaret` is true the caret renders as a
     * tight sibling immediately after the markdown — `-mt-1` + `leading-none`
     * pull it back against the markdown's last baseline so it doesn't read
     * as a detached "row on its own." Inline-with-last-paragraph-text
     * would need a custom react-markdown `p` override (fragile) or DOM
     * injection (worse); the tight sibling is the honest compromise.
     */
    const flushText = (key: string, showCaret: boolean) => {
      if (textBuffer) {
        out.push(
          <div
            key={key}
            className="type-body"
            data-slot={
              showCaret ? "message-event-text-live" : "message-event-text"
            }
          >
            <Markdown>{textBuffer}</Markdown>
          </div>,
        );
        textBuffer = "";
      }
      if (showCaret) {
        out.push(
          <div
            key={`${key}-caret-wrap`}
            className="-mt-1 leading-none"
            data-slot="message-element-caret-wrap"
          >
            <Caret />
          </div>,
        );
      }
    };

    for (let i = 0; i < events.length; i++) {
      const ev = events[i];
      if (ev.kind === "text") {
        textBuffer += ev.delta;
        lastKind = "text";
        continue;
      }
      if (ev.kind === "tool_call") {
        flushText(`text-${textIdx++}`, false);
        // FIFO match: next unconsumed tool_result with matching toolName.
        let resultIdx = -1;
        for (let j = i + 1; j < events.length; j++) {
          const cand = events[j];
          if (
            cand.kind === "tool_result" &&
            cand.toolName === ev.toolName &&
            !consumedResults.has(j)
          ) {
            resultIdx = j;
            break;
          }
        }
        const result =
          resultIdx >= 0
            ? (events[resultIdx] as Extract<
                MessageEvent,
                { kind: "tool_result" }
              >)
            : null;
        if (resultIdx >= 0) consumedResults.add(resultIdx);
        const entry: ToolEntry = {
          toolName: ev.toolName,
          args: ev.args,
          result: result?.content,
          isError: result?.isError,
          pending: !result,
          // Spec 30 T01 (D-30-1): prefer the call's kind; fall back to the
          // matched result's (both carry it when the runtime resolved it).
          kind: ev.toolKind ?? result?.toolKind,
        };
        // R9-045: append the loop index unconditionally — `ev.callId || i`
        // only saved a falsy callId, not a truthy-but-duplicate synthetic
        // `shim-N` id, so ≥2 tool events sharing the same shim id collided
        // on one React key. This is a render-time useMemo interleave, so an
        // index-inclusive key is safe.
        out.push(
          <ToolCallCard
            key={`tool-${ev.callId ?? "noid"}-${i}`}
            entry={entry}
          />,
        );
        // F4 T10: project the tool_call+tool_result pair onto OutputContent[]
        // and emit them inline alongside the tool card. Recognized capability
        // tools (image_gen / code_exec / doc_gen) get rich rendering — chart
        // images, download chips, result blocks. Pending tools surface a
        // shared <WorkingState> via the dispatcher. Unrecognized tools
        // (web_search, file_*) contribute nothing — they already surface via
        // the ToolCallCard.
        const outputs = projectToolEvents(ev.toolName, result);
        for (const o of outputs) {
          out.push(
            <OutputDispatcher
              key={`out-${ev.callId || i}-${o.kind}-${out.length}`}
              personaId={personaId}
              output={o}
              onViewLarger={onViewLarger}
            />,
          );
        }
        pending = result ? null : ev.toolName;
        lastKind = "tool_call";
        continue;
      }
      // tool_result events are matched lazily inside tool_call; pending
      // result-only events without a preceding call get dropped (edge
      // case: compaction-mid stream from Spec-11 fix 5).
      lastKind = "tool_result";
    }
    // Flush trailing text. Show the caret only when streaming AND the most
    // recent stream event was text AND no tool is pending (a pending tool
    // surfaces ToolRunningIndicator instead of the text caret).
    const isLive = streaming && lastKind === "text" && pending === null;
    flushText(`text-${textIdx++}`, isLive);

    return { items: out, pendingToolName: pending, isLiveText: isLive };
  }, [events, streaming, personaId, onViewLarger]);

  // Activity indicator state machine:
  //   - empty + streaming  → thinking
  //   - tool pending       → ToolRunningIndicator
  //   - last was text + streaming → caret next to the inline text span
  //   - otherwise           → none
  if (events.length === 0 && streaming) {
    return (
      <StreamingTextRenderer
        text=""
        streaming
        thinking
        thinkingLabel={thinkingLabel}
      />
    );
  }

  const showToolRunning = streaming && pendingToolName !== null;
  // The gap between rounds: the model is generating (thinking event) but hasn't
  // produced text or a tool call yet — no live caret, no pending tool. Without
  // this the surface looks frozen while a long code_execution call is written.
  const showWorking =
    streaming && working && pendingToolName === null && !isLiveText;

  return (
    <div
      className="flex flex-col gap-2"
      data-slot="message-element-interleaved"
    >
      {items}
      {showToolRunning && pendingToolName ? (
        <ToolRunningIndicator
          label={t("toolRunning", {
            name: personaName,
            tool: pendingToolName,
          })}
        />
      ) : showWorking ? (
        <StreamingTextRenderer
          text=""
          streaming
          thinking
          thinkingLabel={thinkingLabel}
        />
      ) : null}
    </div>
  );
}

/**
 * F4 T10 — project a (tool_call, tool_result?) MessageEvent pair onto
 * `OutputContent[]` for the inline OutputDispatcher. Mirrors the chat
 * normaliser's per-event behaviour but works against the InterleavedContent
 * pair-matched shape (callId-keyed via FIFO matching by toolName).
 *
 *   - Recognized capability tool + result → projectToolResult on a
 *     synthesised ToolResultData (forwarding the structured producedFiles
 *     surfaced by the T02b runtime amendment + use-chat T10 forwarding).
 *   - Recognized capability tool + pending → [{kind: "working", ...}] so
 *     the shared `<WorkingState>` renders inline (D-F4-5).
 *   - Unrecognized tool → [] (ToolCallCard already surfaces it; F4's
 *     output channel is for rich outputs only).
 */
function projectToolEvents(
  toolName: string,
  result: Extract<MessageEvent, { kind: "tool_result" }> | null,
): OutputContent[] {
  const op = operationFor(toolName);
  if (result === null) {
    // Pending: only recognized capability tools show a <WorkingState>.
    return op === null
      ? []
      : [{ kind: "working", operation: op, label: toolName }];
  }
  // Spec 28: ANY tool that persisted artifacts renders file-cards, even when it
  // isn't a recognized F4 capability tool (file_write / render_diagram have no
  // operationFor entry). Recognized tools without artifacts keep the F4 behavior
  // (produced_files / result-block); unrecognized + no artifacts → [].
  const hasArtifacts = (result.artifacts?.length ?? 0) > 0;
  if (!hasArtifacts && op === null) return [];
  return projectToolResult({
    tool_name: toolName,
    is_error: result.isError,
    content: result.content,
    produced_files: result.producedFiles,
    artifacts: result.artifacts,
  });
}

/**
 * Vermilion `--primary` streaming caret. Same shape as the T17 caret;
 * inlined here so it can render next to a text span without going through
 * StreamingTextRenderer's prop dance. Aria-hidden — the streaming text span
 * itself carries the polite live-region.
 */
function Caret() {
  return (
    <span
      aria-hidden="true"
      className="ml-0.5 inline-block h-4 w-[3px] translate-y-0.5 animate-pulse rounded-full bg-primary"
      data-slot="message-element-caret"
    />
  );
}

/**
 * D-F2-15 + (b) tool-running status indicator. Three softly-pulsing dots +
 * the visible italic label "Astrid is using web_search…". Appears when
 * streaming AND a tool_call is awaiting its tool_result. Replaces the
 * previous "between-text silence" with a natural activity cue.
 *
 * Visual treatment matches T17 ThinkingIndicator (intentional — same
 * "the persona is busy, please wait" conversation rhythm): `type-ui` +
 * italic + `text-muted-foreground`; `py-1.5` vertical breathing room;
 * `size-2` dots with a 0/200/400ms pulse stagger for a readable wave;
 * `bg-muted-foreground/70` for a softer "still loading" tone. <output>
 * has implicit role="status" + aria-live (Biome useSemanticElements
 * convention shared with the thinking indicator).
 */
function ToolRunningIndicator({ label }: { label: string }) {
  return (
    <output
      aria-label={label}
      className="type-ui inline-flex items-center gap-2 py-1.5 text-muted-foreground italic"
      data-slot="message-element-tool-running"
    >
      <span aria-hidden="true" className="inline-flex items-center gap-1">
        <span
          className="size-2 animate-pulse rounded-full bg-muted-foreground/70"
          style={{ animationDelay: "0ms" }}
        />
        <span
          className="size-2 animate-pulse rounded-full bg-muted-foreground/70"
          style={{ animationDelay: "200ms" }}
        />
        <span
          className="size-2 animate-pulse rounded-full bg-muted-foreground/70"
          style={{ animationDelay: "400ms" }}
        />
      </span>
      <span>{label}</span>
    </output>
  );
}
