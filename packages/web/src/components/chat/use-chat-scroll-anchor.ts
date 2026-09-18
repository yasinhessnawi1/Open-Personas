"use client";

import {
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";

/**
 * Spec 35: "stick to bottom" follow logic. The reader counts as pinned to the
 * latest when within this many px of the bottom; beyond it, streaming chunks
 * stop auto-scrolling so they can read earlier turns undisturbed.
 */
const PIN_THRESHOLD_PX = 80;

/**
 * `useLayoutEffect` on the client, `useEffect` on the server render pass.
 *
 * ChatWindow is a client component inside a server-rendered route, so React
 * would warn about `useLayoutEffect` during SSR. There is nothing to scroll on
 * the server, so the no-op passive form is the right shape there.
 */
const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

export interface ChatScrollAnchor {
  /** Attach to the scrolling viewport (the `overflow-y-auto` element). */
  scrollerRef: RefObject<HTMLDivElement | null>;
  /** Attach to the element that WRAPS the turns (the thing that grows). */
  contentRef: RefObject<HTMLDivElement | null>;
  /** Wire to the scroller's `onScroll`: recomputes whether the reader is pinned. */
  onScroll: () => void;
  /** Re-arm following, e.g. the reader's own send always returns them to the latest. */
  pinToBottom: () => void;
}

/**
 * Open a conversation on its latest turn, and keep following it only while the
 * reader wants to be followed.
 *
 * Three rules, in the order they matter:
 *
 *  1. **Open at the latest.** Opening a conversation that has history lands on
 *     the newest turn instead of the top of the log (GitHub issue #15). The
 *     jump runs in a LAYOUT effect, so it happens before the browser paints and
 *     the reader never sees the top of the history flash past. It is keyed on
 *     `conversationId`, not on mount, because the App Router REUSES ChatWindow
 *     across `/chat/[id]` (see the reattach effect in `use-chat.ts`): without
 *     the key, switching conversations while scrolled up would open the next
 *     one halfway up its history. Reopening the same conversation therefore
 *     behaves identically every time.
 *
 *  2. **Anchor to the bottom, not to a pixel.** History carries avatars and
 *     Bearer-fetched inline images (`AuthedImage`) that land well after the
 *     first paint and grow the log underneath the reader. A one-shot scroll at
 *     open is undone by that growth, which is what left people short of the
 *     last message. A ResizeObserver on the content re-anchors while pinned, so
 *     "the bottom" means the bottom after everything has loaded.
 *
 *  3. **Never yank a reader who scrolled up.** Every write is guarded by the
 *     pinned flag: new tokens, a background `message.delivered` reload, and
 *     late-loading content all leave `scrollTop` untouched once the reader has
 *     moved away from the bottom, so the browser's own scroll anchoring keeps
 *     their place when content above them changes size.
 *
 * @param conversationId The open conversation, the re-anchor key.
 * @param messages The rendered turns. Only the identity and the length matter.
 * @returns The refs and handlers ChatWindow wires onto its scroller.
 */
export function useChatScrollAnchor(
  conversationId: string,
  messages: readonly unknown[],
): ChatScrollAnchor {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  // True while the reader is near the bottom and should follow new content.
  // Flipped false the moment they scroll up; re-armed when they return or send.
  // Ref-only, so scrolling never costs a re-render.
  const pinnedRef = useRef(true);
  // The conversation we have already opened at its latest turn.
  const openedRef = useRef<string | null>(null);

  const jumpToBottom = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    // A log that fits inside its viewport has no bottom to go to (an empty or
    // one-turn conversation). Writing scrollTop there is a no-op in a browser,
    // but claiming to have moved the reader is not the behaviour we want to
    // hold a test to.
    if (el.scrollHeight <= el.clientHeight) return;
    // Instant, not smooth: across rapid chunk updates a smooth scroll fights
    // itself, and at open there is no motion to animate in the first place.
    el.scrollTop = el.scrollHeight;
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    pinnedRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < PIN_THRESHOLD_PX;
  }, []);

  const pinToBottom = useCallback(() => {
    pinnedRef.current = true;
  }, []);

  // Rule 1: open at the latest turn. An empty conversation has no latest turn,
  // so it is left exactly where it is (and stays unopened, so the first message
  // to arrive still lands us at the bottom).
  const hasHistory = messages.length > 0;
  useIsomorphicLayoutEffect(() => {
    if (openedRef.current === conversationId) return;
    if (!hasHistory) return;
    openedRef.current = conversationId;
    pinnedRef.current = true;
    jumpToBottom();
  }, [conversationId, hasHistory, jumpToBottom]);

  // Rule 3: follow the newest content ONLY while pinned, so streaming doesn't
  // drag the reader down while they scroll up to re-read.
  useIsomorphicLayoutEffect(() => {
    if (!pinnedRef.current) return;
    jumpToBottom();
  }, [messages, jumpToBottom]);

  // Rule 2: content that finishes loading after the turn rendered (avatars,
  // Bearer-fetched images, a chart that sizes itself) re-anchors us to the
  // bottom while pinned. Fail-soft where ResizeObserver is absent: the follow
  // effect above still covers every message-driven change.
  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (pinnedRef.current) jumpToBottom();
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [jumpToBottom]);

  return { scrollerRef, contentRef, onScroll, pinToBottom };
}
