"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";
import type { ChatMessageView } from "@/components/chat/message-element";
import { ApiError, createApiClient, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { persistedToView, reduceChatEvent } from "@/lib/chat/reduce-chat-event";
import { consumeSSE, type RawSSEEvent } from "@/lib/sse";
import type { ProactiveProposal } from "@/lib/sse-types";
import { parseChatEvent } from "@/lib/sse-types";
import { useActiveWork } from "@/lib/work/active-work-context";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * F3 (T06) — workspace reference for an attached image. Mirrors the API's
 * `ImageRef` shape exactly; the composer (T19) maps successful uploads
 * (`ImageAttachment.state === "success"`) onto this shape before passing
 * the array to `send()`. Store-by-reference: NEVER a base64 data URI.
 */
export type ImageRef = components["schemas"]["ImageRef"];

/**
 * Spec 35 — a document attached to a turn, carried on the optimistic user
 * message so the file is visible in the thread. Display-only (the backend reads
 * the document from conversation context; it isn't re-sent in the request body).
 */
export type AttachedDoc = {
  doc_ref: string;
  filename: string;
  format: string;
  size_bytes: number | null;
  strategy?: "whole_inject" | "retrieval" | "vision_handoff";
};

type Patch = (fn: (a: ChatMessageView) => ChatMessageView) => void;

/**
 * Apply ONE SSE frame from a turn's stream to the assistant turn (Spec P1 T7).
 *
 * Extracted so the originating POST stream (`send`) AND the reattach tail
 * (`reattach` → `…/active-turn/events`) share the exact same frame handling —
 * the reattach is the same proven streaming code, not a fork. Returns `"error"`
 * when the worker emitted an `error` frame (a turn that failed server-side) so
 * the caller can surface it and stop; `"ok"` otherwise.
 */
function applyTurnFrame(raw: RawSSEEvent, patch: Patch): "ok" | "error" {
  // The detached worker emits an `error` frame on a server-side turn failure
  // (Spec P1) before ending the stream; surface it rather than silently stopping.
  if (raw.event === "error") return "error";
  const ev = parseChatEvent(raw);
  if (!ev) return "ok";
  // Spec P3 (P3-D-3): the live path folds through the SAME pure `reduceChatEvent`
  // the persisted-log reconstruction uses — one reducer, so the live union and the
  // persisted shape cannot drift. The reduction logic is unchanged from the old
  // inline body (verified byte-identical render).
  patch((a) => reduceChatEvent(a, ev));
  return "ok";
}

/**
 * Chat state + SSE streaming (spec §4.2) + persistent/resumable turns (Spec P1).
 *
 * On send: optimistically append the user turn + a streaming assistant turn,
 * then consume the SSE stream from the detached turn (`chunk` → accumulate,
 * `tool_calling`/`tool_result` → cards, `done` → tier).
 *
 * Spec P1 — the turn now runs server-side detached, so it survives navigation:
 * - The active stream's `AbortController` is aborted on UNMOUNT (navigate away),
 *   which stops the fetch but NOT the server-side turn — it keeps running.
 * - On MOUNT/return, `reattach()` asks `GET …/active-turn`; if a turn is live it
 *   marks the assistant turn streaming and resubscribes to `…/active-turn/events`
 *   (the same frame handling as send), then RECONCILES via the persisted history
 *   on stream end so the final content is authoritative (no gap/double survives a
 *   completed turn).
 */
export function useChat(
  conversationId: string,
  initial: ChatMessageView[],
  personaId: string,
) {
  const { getToken } = useAuth();
  const [messages, setMessages] = useState<ChatMessageView[]>(initial);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState(false);
  // Spec 30 (D-30-2): the last user message, so an in-chat consent accept can
  // re-send it (surface-and-retry) once the capability is granted.
  const lastUserMessage = useRef<string>("");
  // Spec P1: the active turn's fetch controller — aborted on unmount so a
  // navigate-away stops the stream WITHOUT cancelling the detached server turn.
  const abortRef = useRef<AbortController | null>(null);
  // Spec P3 (P3-D-5b): a ref mirror of `streaming`, so the reattach in-flight
  // guard reads the CURRENT value synchronously. `send`'s disconnect-recovery
  // calls reattach from its catch right after `setStreaming(false)` — the state
  // update hasn't re-rendered yet, so a guard on the `streaming` state closure
  // would still read `true` and bail. The ref is set imperatively at those seams.
  const streamingRef = useRef(false);
  // Latest `reattach`, so `send`'s 409 path + the mount effect call it without a
  // dependency cycle / re-running on every `streaming` toggle.
  const reattachRef = useRef<() => Promise<void>>(async () => {});

  // Spec P1 (D-P1-v7-indicator): advertise the in-progress turn to the app-level
  // active-work session so the conversation row / global bar show a "working"
  // cue. Register while streaming; unregister when the turn ENDS WHILE MOUNTED.
  // Deliberately NO unmount cleanup — a navigate-away must KEEP the indicator (the
  // turn keeps running server-side); the provider's poll clears it on completion.
  const { registerChat, unregisterChat } = useActiveWork();
  useEffect(() => {
    streamingRef.current = streaming;
    if (streaming) registerChat({ conversationId, personaId });
    else unregisterChat(conversationId);
  }, [streaming, conversationId, personaId, registerChat, unregisterChat]);

  const token = useCallback(
    () => getToken(TEMPLATE ? { template: TEMPLATE } : undefined),
    [getToken],
  );

  const reload = useCallback(async () => {
    const jwt = await token();
    const client = createApiClient(() => Promise.resolve(jwt));
    const conv = await unwrap(
      await client.GET("/v1/conversations/{conversation_id}", {
        params: { path: { conversation_id: conversationId } },
      }),
    );
    // Read + validate the history SYNCHRONOUSLY here (in the async body, NOT inside
    // the setMessages updater): a malformed / empty GET that lacks `messages` throws
    // on THIS line, where it rejects the `reload` promise and is swallowed by the
    // caller's `.catch`. If the `.map` were the FIRST contact with a missing
    // `messages`, that throw would happen later inside the React render phase (the
    // updater runs during render) — uncatchable, crashing the whole hook (and any
    // turn already rendered, e.g. a just-landed proactive rail). Validating up front
    // keeps the failure inside the catchable async boundary.
    const incoming = conv.messages;
    if (!Array.isArray(incoming)) {
      throw new Error("reload: conversation GET returned no messages array");
    }
    // Spec P3 (P3-D-4/5a): reconstruct the interleaved view from the persisted
    // ordered log via the SHARED `persistedToView` — the one mapper that replaced
    // the old two divergent text-only maps. This is what makes a refresh /
    // conversation-switch / reconnect-reconcile reproduce the rich turn instead of
    // flattening it to text (the "disappears by itself" bug). On the P1 reattach
    // reconcile (stream end) this STRICTLY IMPROVES the old behaviour — the
    // authoritative final still wins, it just no longer drops the rich content.
    setMessages((prev) => {
      const prevById = new Map(prev.map((m) => [m.id, m]));
      return incoming.map((m) => {
        // P3-D-5b merge-not-replace: when the persisted log is absent (legacy /
        // NULL row) but we already hold a rich in-memory turn for this id, KEEP
        // the rich version rather than overwriting it with the text-only render.
        // (A just-streamed turn always has `events`, so this guards mixed
        // histories — the literal "preserve already-rendered rich turns" rule.)
        if (!m.events || m.events.length === 0) {
          const prevMsg = prevById.get(m.id);
          if (
            prevMsg &&
            ((prevMsg.events?.length ?? 0) > 0 ||
              (prevMsg.tools?.length ?? 0) > 0)
          ) {
            return prevMsg;
          }
        }
        return persistedToView(m);
      });
    });
    // R9-025 leg C: the RAW server list (real persisted ids), returned so
    // `regenerate` / `editAndRerun` can resolve their tail target's REAL id
    // synchronously — `setMessages` above is React-scheduled, so the `messages`
    // state closure isn't updated yet when this function returns.
    return incoming;
  }, [conversationId, token]);

  const send = useCallback(
    async (
      content: string,
      attachedImages: ImageRef[] = [],
      attachedDocs: AttachedDoc[] = [],
    ) => {
      if (!content.trim() || streaming) return;
      setError(false);
      lastUserMessage.current = content;
      const userId = crypto.randomUUID();
      const asstId = crypto.randomUUID();
      setMessages((m) => [
        ...m,
        // F3 (T06): the optimistic user-turn carries `images` so the bubble can
        // render the just-attached image inline before the server echoes it back.
        // Spec 35: `documents` ride the same optimistic turn (display-only).
        {
          id: userId,
          role: "user",
          content,
          images: attachedImages.length > 0 ? attachedImages : undefined,
          documents: attachedDocs.length > 0 ? attachedDocs : undefined,
        },
        {
          id: asstId,
          role: "assistant",
          content: "",
          tools: [],
          // F2 D-F2-15: events[] preserves stream order for interleaved render.
          events: [],
          streaming: true,
        },
      ]);
      setStreaming(true);
      streamingRef.current = true;

      const patch: Patch = (fn) =>
        setMessages((m) => m.map((msg) => (msg.id === asstId ? fn(msg) : msg)));

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        const jwt = await token();
        // F3 (T06) — store-by-reference: omit `images` entirely (NOT `[]`) when
        // empty (the server's min_length=1 validator rejects an empty list). The
        // body carries ONLY workspace_path + media_type, never base64 bytes.
        const requestBody: { content: string; images?: ImageRef[] } = {
          content,
        };
        if (attachedImages.length > 0) requestBody.images = attachedImages;
        for await (const raw of consumeSSE(
          `${API}/v1/conversations/${conversationId}/messages`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${jwt}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify(requestBody),
            signal: ctrl.signal,
          },
        )) {
          if (applyTurnFrame(raw, patch) === "error") {
            setError(true);
            break;
          }
        }
        patch((a) => ({ ...a, streaming: false, working: false }));
        setStreaming(false);
        streamingRef.current = false;
      } catch (e) {
        // Navigate-away aborts the fetch — the detached turn keeps running; do
        // NOT mark the turn failed or reload (the next mount reattaches).
        if ((e as Error)?.name === "AbortError") return;
        setStreaming(false);
        streamingRef.current = false;
        patch((a) => ({ ...a, streaming: false, working: false }));
        // Release THIS turn's controller now so the recovery reattach below isn't
        // bailed by the in-flight guard (the `finally` also clears it — harmless
        // double; clearing here is what makes the 409 + disconnect reattach reach
        // the active-turn check instead of no-op'ing on a stale controller).
        if (abortRef.current === ctrl) abortRef.current = null;
        // A 409 means a turn is already active for this conversation (one-active-
        // turn): reattach to it rather than showing an error.
        if (e instanceof ApiError && e.status === 409) {
          await reattachRef.current();
          return;
        }
        setError(true);
        // Spec P3 (P3-D-5b) — true-disconnect-only recovery: a non-ApiError throw
        // is a transport disconnect, and the detached server turn may STILL be
        // running (P1). Route through reattach — resubscribe to the live tail if
        // it's live; if it already finished, the active-turn 404 falls through to
        // reattach's now-rich reconcile (`reload` via `persistedToView`). A clean
        // ApiError (e.g. 429) is NOT a disconnect — keep the optimistic turn so it
        // can retry (no reload, no reattach).
        if (!(e instanceof ApiError))
          await reattachRef.current().catch(() => {});
      } finally {
        if (abortRef.current === ctrl) abortRef.current = null;
      }
    },
    [conversationId, streaming, token],
  );

  /**
   * R9-025 leg C — regenerate: a REAL server-side retry of the tail assistant
   * reply (`POST …/messages/{id}/regenerate`), replacing the wave-2a
   * client-side echo-resend the owner rejected ("the LLM is now confused" —
   * the old reply was never excluded from context). The API supersedes the
   * target and re-runs the SAME preceding user turn with the old reply
   * provably absent from the prompt.
   *
   * `reload()` runs FIRST: `send()`'s optimistic assistant turn carries a
   * CLIENT-side random id until the next natural reload reconciles it with
   * the server's real persisted id (`send()` itself never reloads on clean
   * completion) — the MOST common regenerate click (right after a reply just
   * finished streaming) would otherwise target an id the server has never
   * heard of. Falls back to the CURRENT last message when the clicked id
   * isn't found post-reload (that stale-id case) — chat-window.tsx only ever
   * wires `onRetryMessage` onto the tail assistant message (v1 scope), so
   * "whatever is now last" IS what was clicked, reconciled.
   *
   * Streams the SAME frame shapes as `send()` via the identical
   * `applyTurnFrame` reducer, replacing the target bubble'S content IN PLACE
   * (same array slot, same id) rather than appending a new one — the visual
   * "replaced, not duplicated" contract the api's supersede filtering backs.
   */
  const regenerate = useCallback(
    async (assistantMessageId: string) => {
      if (streaming) return;
      setError(false);
      const fresh = await reload().catch(() => null);
      if (!fresh) {
        setError(true);
        return;
      }
      const target =
        fresh.find((m) => m.id === assistantMessageId) ??
        fresh[fresh.length - 1];
      if (!target || target.role !== "assistant") return;
      const targetId = target.id;

      setMessages((m) =>
        m.map((msg) =>
          msg.id === targetId
            ? {
                ...msg,
                content: "",
                tools: [],
                events: [],
                streaming: true,
                working: false,
              }
            : msg,
        ),
      );
      setStreaming(true);
      streamingRef.current = true;

      const patch: Patch = (fn) =>
        setMessages((m) =>
          m.map((msg) => (msg.id === targetId ? fn(msg) : msg)),
        );

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        const jwt = await token();
        for await (const raw of consumeSSE(
          `${API}/v1/conversations/${conversationId}/messages/${targetId}/regenerate`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${jwt}` },
            signal: ctrl.signal,
          },
        )) {
          if (applyTurnFrame(raw, patch) === "error") {
            setError(true);
            break;
          }
        }
        patch((a) => ({ ...a, streaming: false, working: false }));
        setStreaming(false);
        streamingRef.current = false;
        // Reconcile the NEW reply's real id + persisted content — the clean-
        // completion reload send() itself skips, but regenerate needs (the
        // NEXT regenerate click must resolve a REAL id again).
        await reload().catch(() => {});
      } catch (e) {
        if ((e as Error)?.name === "AbortError") return;
        setStreaming(false);
        streamingRef.current = false;
        patch((a) => ({ ...a, streaming: false, working: false }));
        if (abortRef.current === ctrl) abortRef.current = null;
        if (e instanceof ApiError && e.status === 409) {
          await reattachRef.current();
          return;
        }
        setError(true);
        if (!(e instanceof ApiError))
          await reattachRef.current().catch(() => {});
      } finally {
        if (abortRef.current === ctrl) abortRef.current = null;
      }
    },
    [conversationId, streaming, token, reload],
  );

  /**
   * R9-025 leg C — edit-and-rerun: save an edit to the LAST user message and
   * immediately re-run (`PATCH …/messages/{id}/edit`). The API supersedes the
   * old user+reply pair and starts a brand-new turn on the edited content —
   * the OLD wording is excluded from the next prompt exactly like a
   * regenerated reply is.
   *
   * Same `reload()`-first id reconciliation as `regenerate` (see its
   * docstring) — PLUS a position-based fallback when the clicked id isn't
   * found post-reload: falls back to whichever message IS the current last
   * user message (the true tail, or the second-to-last when the tail is that
   * message's own assistant reply — the normal completed-turn shape).
   * message-element.tsx only ever wires `onEditMessage` onto that ONE message
   * (v1 tail-only scope), so this recovers the common "just sent, never
   * reloaded" case rather than silently dropping the edit.
   */
  const editAndRerun = useCallback(
    async (userMessageId: string, newContent: string) => {
      const trimmed = newContent.trim();
      if (!trimmed || streaming) return;
      setError(false);
      const fresh = await reload().catch(() => null);
      if (!fresh) {
        setError(true);
        return;
      }

      let targetIdx = fresh.findIndex((m) => m.id === userMessageId);
      if (targetIdx === -1 || fresh[targetIdx]?.role !== "user") {
        if (fresh.length > 0 && fresh[fresh.length - 1].role === "user") {
          targetIdx = fresh.length - 1;
        } else if (
          fresh.length > 1 &&
          fresh[fresh.length - 1].role === "assistant" &&
          fresh[fresh.length - 2].role === "user"
        ) {
          targetIdx = fresh.length - 2;
        } else {
          targetIdx = -1;
        }
      }
      if (targetIdx === -1) return;
      const targetId = fresh[targetIdx].id;
      const hasTrailingReply =
        targetIdx === fresh.length - 2 &&
        fresh[fresh.length - 1]?.role === "assistant";
      const asstId = hasTrailingReply
        ? fresh[fresh.length - 1].id
        : crypto.randomUUID();

      setMessages((prev) => {
        const base = prev.map((msg) =>
          msg.id === targetId ? { ...msg, content: trimmed } : msg,
        );
        if (hasTrailingReply) {
          return base.map((msg) =>
            msg.id === asstId
              ? {
                  ...msg,
                  content: "",
                  tools: [],
                  events: [],
                  streaming: true,
                  working: false,
                }
              : msg,
          );
        }
        return [
          ...base,
          {
            id: asstId,
            role: "assistant" as const,
            content: "",
            tools: [],
            events: [],
            streaming: true,
          },
        ];
      });
      setStreaming(true);
      streamingRef.current = true;

      const patch: Patch = (fn) =>
        setMessages((m) => m.map((msg) => (msg.id === asstId ? fn(msg) : msg)));

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        const jwt = await token();
        for await (const raw of consumeSSE(
          `${API}/v1/conversations/${conversationId}/messages/${targetId}/edit`,
          {
            method: "PATCH",
            headers: {
              Authorization: `Bearer ${jwt}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ content: trimmed }),
            signal: ctrl.signal,
          },
        )) {
          if (applyTurnFrame(raw, patch) === "error") {
            setError(true);
            break;
          }
        }
        patch((a) => ({ ...a, streaming: false, working: false }));
        setStreaming(false);
        streamingRef.current = false;
        await reload().catch(() => {});
      } catch (e) {
        if ((e as Error)?.name === "AbortError") return;
        setStreaming(false);
        streamingRef.current = false;
        patch((a) => ({ ...a, streaming: false, working: false }));
        if (abortRef.current === ctrl) abortRef.current = null;
        if (e instanceof ApiError && e.status === 409) {
          await reattachRef.current();
          return;
        }
        setError(true);
        if (!(e instanceof ApiError))
          await reattachRef.current().catch(() => {});
      } finally {
        if (abortRef.current === ctrl) abortRef.current = null;
      }
    },
    [conversationId, streaming, token, reload],
  );

  // Spec P1 — reattach to a live turn on mount/return. Detect via
  // `GET …/active-turn` (404 ⇒ nothing live), mark the assistant turn streaming,
  // resubscribe to the live tail, then reconcile via persisted history on end.
  const reattach = useCallback(async () => {
    // P3-D-5b: guard on the ref (not the `streaming` state closure) so a reattach
    // invoked from `send`'s disconnect catch — right after `setStreaming(false)`,
    // before the re-render — sees the current value instead of a stale `true`.
    if (streamingRef.current || abortRef.current) return;
    const jwt = await token();
    let active: {
      message_id: string;
      streaming_status: string;
      content: string;
    } | null = null;
    try {
      const res = await fetch(
        `${API}/v1/conversations/${conversationId}/active-turn`,
        { headers: { Authorization: `Bearer ${jwt}` } },
      );
      if (res.status === 404) return; // no live turn — normal mount
      if (!res.ok) return;
      active = await res.json();
    } catch {
      return; // detection is best-effort; a failure just means "no reattach"
    }
    if (!active) return;
    const asstId = active.message_id;

    setStreaming(true);
    streamingRef.current = true;
    // Seed: mark the in-progress assistant row (already present from the
    // server-fetched history) as streaming. Its content is the persisted
    // checkpoint; the live tail APPENDS new deltas, and the reconcile on end
    // replaces it with the authoritative final — so a completed turn never shows
    // a gap or a double.
    setMessages((m) => {
      const exists = m.some((msg) => msg.id === asstId);
      const seeded = m.map((msg) =>
        msg.id === asstId ? { ...msg, streaming: true, working: true } : msg,
      );
      return exists
        ? seeded
        : [
            ...seeded,
            {
              id: asstId,
              role: "assistant" as const,
              content: active?.content ?? "",
              tools: [],
              events: [],
              streaming: true,
            },
          ];
    });
    const patch: Patch = (fn) =>
      setMessages((m) => m.map((msg) => (msg.id === asstId ? fn(msg) : msg)));

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      for await (const raw of consumeSSE(
        `${API}/v1/conversations/${conversationId}/active-turn/events`,
        { headers: { Authorization: `Bearer ${jwt}` }, signal: ctrl.signal },
      )) {
        if (applyTurnFrame(raw, patch) === "error") {
          setError(true);
          break;
        }
      }
      patch((a) => ({ ...a, streaming: false, working: false }));
      setStreaming(false);
      streamingRef.current = false;
      // Reconcile: the persisted final is authoritative (covers the throttled-
      // checkpoint seed boundary). Never resume the raw SSE. Spec P3 (P3-D-5a):
      // `reload` now reconstructs the rich interleaved view via `persistedToView`,
      // so this reconcile PRESERVES tool cards / artifacts instead of flattening
      // the just-tailed turn to text — the must-not-regress guard for the P1
      // reattach reconcile.
      await reload().catch(() => {});
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return; // unmount; turn keeps running
      setStreaming(false);
      streamingRef.current = false;
      patch((a) => ({ ...a, streaming: false, working: false }));
      // 404 ⇒ the turn finished between detect and tail; reconcile (now rich).
      await reload().catch(() => {});
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
    }
  }, [conversationId, token, reload]);

  // Keep the ref pointing at the latest `reattach` (closes over current state).
  reattachRef.current = reattach;

  // On mount/return: try to reattach to a live turn (ONCE per conversation —
  // keyed on the id, not on `reattach`, so a `streaming` toggle doesn't re-fire
  // it). On unmount (navigate away): abort the active fetch — the detached server
  // turn keeps running, re-tailable on the next mount. conversationId is the
  // intended trigger (App Router reuses this component across /chat/[id], so the
  // effect must re-fire on id change); the body reads the latest reattach via a
  // ref by design.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above — conversationId is the deliberate re-fire key; reattach is read via ref.
  useEffect(() => {
    void reattachRef.current();
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [conversationId]);

  // Spec 30 (D-30-2): grant a capability the runtime offered (the rail's accept
  // path). Persisting the grant is what makes the retry effective.
  const grantCapability = useCallback(
    async (toolName: string) => {
      const jwt = await token();
      const client = createApiClient(() => Promise.resolve(jwt));
      await unwrap(
        await client.POST("/v1/personas/{persona_id}/tools", {
          params: { path: { persona_id: personaId } },
          body: { tool_name: toolName },
        }),
      );
    },
    [personaId, token],
  );

  // Spec 30 (D-30-2): answer an in-chat proactive question. The enable option
  // grants the capability then RE-SENDS the prior user message (surface-and-
  // retry). Every other answer is just the next user message.
  const respondToProactive = useCallback(
    async (
      messageId: string,
      answer: string,
      opts: { isAccept: boolean; proposal?: ProactiveProposal },
    ) => {
      setMessages((m) =>
        m.map((msg) =>
          msg.id === messageId ? { ...msg, proactive: undefined } : msg,
        ),
      );
      if (opts.isAccept && opts.proposal?.action === "grant_tool") {
        await grantCapability(opts.proposal.name);
        await send(lastUserMessage.current);
        return;
      }
      await send(answer);
    },
    [grantCapability, send],
  );

  return {
    messages,
    streaming,
    error,
    send,
    reload,
    reattach,
    respondToProactive,
    regenerate,
    editAndRerun,
  };
}
