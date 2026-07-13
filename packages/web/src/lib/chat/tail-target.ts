/**
 * R9-025 leg C — v1 conversation-TAIL-only scope: which rendered message (if
 * any) is eligible for regenerate (the last assistant message) or edit (the
 * last user message, allowing for its own trailing assistant reply — the
 * normal completed-turn shape). Mirrors the api's
 * `chat_service._tail_target` rule exactly, applied to the CLIENT's rendered
 * message list, so the retry/edit action-bar buttons render ONLY on the tail
 * — no branching of older history (a future tree-semantics feature).
 *
 * Pure + framework-free (no React) so it's directly unit-testable; chat-
 * window.tsx is the sole caller (its message-map loop uses these to decide
 * whether to wire `onRetryMessage` / `onEditMessage` for each row, or pass
 * `undefined` — message-action-bar.tsx's existing "omit to hide" contract).
 */

export interface TailMessageLike {
  role: string;
}

/** True iff `messages[index]` is the conversation's CURRENT last assistant message. */
export function isLastAssistantMessage(
  messages: readonly TailMessageLike[],
  index: number,
): boolean {
  return messages[index]?.role === "assistant" && index === messages.length - 1;
}

/**
 * True iff `messages[index]` is the conversation's CURRENT last user message —
 * either the true last message, or the second-to-last when the last is that
 * user message's OWN assistant reply (the normal completed-turn shape; that
 * reply is superseded too on an edit).
 */
export function isLastUserMessage(
  messages: readonly TailMessageLike[],
  index: number,
): boolean {
  if (messages[index]?.role !== "user") return false;
  if (index === messages.length - 1) return true;
  return (
    index === messages.length - 2 &&
    messages[messages.length - 1]?.role === "assistant"
  );
}
