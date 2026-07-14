/**
 * Sidebar data model + the recency ranking shared with the dashboard.
 *
 * The richer app-sidebar surfaces two derived lists from data the app already
 * fetches (no new endpoint, no recency schema):
 *
 *   - PERSONAS: the most-recently-*used* personas (then most-recently-created),
 *     rendered as a compact avatar rail for fast access.
 *   - MESSAGES: the caller's conversations (already `updated_at DESC`), rendered
 *     as a chat-app list. `GET /v1/conversations` returns a summary only
 *     (id / persona_id / title / timestamps) — there is no last-message-author
 *     or message-preview field on the list row, so the row's brief line is the
 *     conversation `title` (the human-readable thread label) and the title line
 *     is the persona name. Surfacing the literal last message would require an
 *     N+1 fetch of `GET /v1/conversations/:id` per row, which the list view
 *     deliberately avoids.
 *
 * `rankPersonasByRecency` is the same derivation `src/app/page.tsx` performs
 * inline for the dashboard — extracted here so both surfaces stay coherent and
 * the ranking is unit-testable in isolation.
 */

import type { AvatarPersona } from "@/components/persona/persona-avatar";

/** Minimum persona shape the sidebar + ranking need (a `PersonaSummary` superset). */
export interface SidebarPersona extends AvatarPersona {
  readonly role: string;
  readonly created_at: string;
}

/** Minimum conversation shape (a `ConversationSummary`). */
export interface SidebarConversationInput {
  readonly id: string;
  readonly persona_id: string;
  readonly title: string;
  readonly updated_at: string;
  /**
   * The conversation's immutable birth-marker (Spec V9): `'chat'` or `'call'`.
   * Call conversations belong under the Calls surface, not the Messages list
   * (R4 T4) — they render there as empty entries.
   */
  readonly origin?: "chat" | "call";
  /**
   * Speaker role of the most recent message; `null`/absent when the conversation
   * has no messages yet. The presence of a role is the "has ≥1 message" signal
   * used to hide empty conversations from the Messages list (R4 T4).
   */
  readonly last_message_role?: "user" | "assistant" | "system" | "tool" | null;
}

/** A resolved message row: a conversation joined to its persona (if known). */
export interface SidebarConversation {
  readonly id: string;
  readonly title: string;
  readonly updated_at: string;
  readonly persona: SidebarPersona | null;
}

/** Minimum call shape (a `CallSummary`). Spec V9 — the Calls surface.
 * `duration_s` is optional-and-nullable on the wire (a live call has no end);
 * `resolveCalls` normalises the absent case to `null`. */
export interface SidebarCallInput {
  readonly call_id: string;
  readonly conversation_id: string;
  readonly persona_id: string;
  readonly started_at: string;
  readonly duration_s?: number | null;
}

/**
 * A resolved call row: a call-record joined to its persona (if known). Each row
 * links to its saved transcript at `/chat/{conversationId}` — the spoken turns
 * persist as conversation messages (V9-D-1/D-2), so the existing chat page
 * renders them.
 */
export interface SidebarCall {
  readonly callId: string;
  readonly conversationId: string;
  readonly startedAt: string;
  readonly durationS: number | null;
  readonly persona: SidebarPersona | null;
}

/**
 * The owner's nav-badge totals (R9-010) — `GET /v1/me/nav-counts`, resolved
 * server-side with the rest of the sidebar data (one round-trip, RLS-scoped).
 * Honest TOTALS, not preview-list lengths: `personas`/`conversations` were
 * previously derived from the truncated rail/messages previews (capped at
 * 4 / 30). `activeTasks` is the non-terminal working set; `schedules` counts
 * schedule ROWS (a recurring schedule counts once, never occurrences);
 * `memoryNodes` counts canonical graph nodes. Fail-soft: a failed fetch reads
 * all-zero, which renders as no badges (zero-hidden).
 */
export interface SidebarNavCounts {
  readonly personas: number;
  readonly conversations: number;
  readonly calls: number;
  readonly memoryNodes: number;
  readonly activeTasks: number;
  readonly schedules: number;
}

/** The all-zero fail-soft counts (no badges rendered). */
export const EMPTY_NAV_COUNTS: SidebarNavCounts = {
  personas: 0,
  conversations: 0,
  calls: 0,
  memoryNodes: 0,
  activeTasks: 0,
  schedules: 0,
};

/** The serialisable bundle the server shell hands to the client sidebar. */
export interface SidebarData {
  readonly personas: readonly SidebarPersona[];
  readonly conversations: readonly SidebarConversation[];
  readonly calls: readonly SidebarCall[];
  /** Nav-badge totals (R9-010); all-zero when the counts fetch failed. */
  readonly counts: SidebarNavCounts;
  /**
   * R4 T1 / Spec K6: the account owner's display name resolved server-side from
   * our own DB (`/v1/me/profile`, given_name + family_name) — the source of
   * truth for the name shown in the account menu, surfaced in BOTH editions.
   * `null` when the owner has set no name.
   */
  readonly ownerName: string | null;
  /**
   * Spec K5: whether this deployment has a usable knowledge-graph (Memory). A
   * runtime signal — the API's window reports `available: false` when there is no
   * Postgres graph store (community-on-SQLite / graph off) — so the nav gates the
   * Memory row by availability, not by hardcoded edition.
   */
  readonly memoryAvailable: boolean;
}

/**
 * Rank personas "most recently used first" using only existing data.
 *
 * `conversations` is assumed `updated_at DESC` (the API guarantees this), so the
 * first appearance of each `persona_id` marks its most-recent activity. Personas
 * never talked to fall to the tail, most-recently-created first. Pure + stable.
 */
export function rankPersonasByRecency(
  personas: readonly SidebarPersona[],
  conversations: readonly SidebarConversationInput[],
): readonly SidebarPersona[] {
  const byId = new Map(personas.map((p) => [p.id, p]));
  const used: SidebarPersona[] = [];
  const seen = new Set<string>();
  for (const c of conversations) {
    const p = byId.get(c.persona_id);
    if (p && !seen.has(p.id)) {
      seen.add(p.id);
      used.push(p);
    }
  }
  const unused = personas
    .filter((p) => !seen.has(p.id))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  return [...used, ...unused];
}

/**
 * Whether a conversation belongs in the MESSAGES list (R4 T4).
 *
 * Two exclusions, both signalled by the `ConversationSummary` the list already
 * returns — no extra fetch:
 *   - CALL conversations (`origin === 'call'`) belong under the Calls surface;
 *     in Messages they render as empty, persona-less rows.
 *   - EMPTY conversations (no messages yet → `last_message_role` is null/absent)
 *     are noise: a started-but-never-used thread.
 * A conversation with `origin` absent (older rows / pre-V9) defaults to chat.
 */
export function isMessagesListConversation(
  c: SidebarConversationInput,
): boolean {
  if (c.origin === "call") return false;
  return c.last_message_role != null;
}

/**
 * Resolve conversation summaries into message rows joined to their persona.
 * Order is preserved (the API already returns `updated_at DESC`). Call + empty
 * conversations are filtered out of the Messages list (R4 T4).
 */
export function resolveConversations(
  conversations: readonly SidebarConversationInput[],
  personas: readonly SidebarPersona[],
): readonly SidebarConversation[] {
  const byId = new Map(personas.map((p) => [p.id, p]));
  return conversations.filter(isMessagesListConversation).map((c) => ({
    id: c.id,
    title: c.title,
    updated_at: c.updated_at,
    persona: byId.get(c.persona_id) ?? null,
  }));
}

/**
 * Resolve call summaries into call rows joined to their persona. Order is
 * preserved (`GET /v1/calls` already returns newest-first by `started_at`).
 */
export function resolveCalls(
  calls: readonly SidebarCallInput[],
  personas: readonly SidebarPersona[],
): readonly SidebarCall[] {
  const byId = new Map(personas.map((p) => [p.id, p]));
  return calls.map((c) => ({
    callId: c.call_id,
    conversationId: c.conversation_id,
    startedAt: c.started_at,
    durationS: c.duration_s ?? null,
    persona: byId.get(c.persona_id) ?? null,
  }));
}

/**
 * R9-038 — the rail's route-derived live-presence state.
 *
 * `conversationId → personaId`, built from the SAME resolved lists the
 * sidebar already renders (`MessagesList`'s `conversations`, `CallsList`'s
 * `calls`) — no extra fetch. Both are consulted because a call-origin
 * conversation never appears in `conversations` (R4 T4 excludes it from the
 * Messages list), so `/chat/{id}/voice` would otherwise be unresolvable.
 */
export function buildConversationPersonaMap(
  conversations: readonly SidebarConversation[],
  calls: readonly SidebarCall[],
): ReadonlyMap<string, string> {
  const map = new Map<string, string>();
  for (const c of conversations) if (c.persona) map.set(c.id, c.persona.id);
  for (const c of calls) if (c.persona) map.set(c.conversationId, c.persona.id);
  return map;
}

/** Route-derived presence: which persona (if any) the caller is on-call with / in-chat with. */
export interface PersonaRoutePresence {
  readonly activePersonaId: string | null;
  readonly onCallPersonaId: string | null;
}

const CHAT_OR_VOICE_ROUTE = /^\/chat\/([^/]+)(\/voice)?\/?$/;
const PERSONA_PAGE_ROUTE = /^\/personas\/([^/]+)/;

/**
 * Derive `{activePersonaId, onCallPersonaId}` from the current pathname.
 *
 * `/chat/{id}/voice` → on-call with the conversation's persona (a real V4
 * call session isn't consulted — the route IS the contract, matching how
 * `MessagesList`/`CallsList` already derive their own `active` highlight
 * from `pathname` alone). `/chat/{id}` (any other/no subpath) → in-chat.
 * `/personas/{id}` (and any nested subpath — edit/files) → in-chat via the
 * persona page, EXCEPT `/personas/new` (no persona yet). A conversation id
 * absent from `conversationPersonaMap` (outside the sidebar's fetched
 * preview) resolves to no presence — the same bounded-data limitation the
 * rest of the sidebar already accepts.
 */
export function derivePersonaRoutePresence(
  pathname: string,
  conversationPersonaMap: ReadonlyMap<string, string>,
): PersonaRoutePresence {
  const chatMatch = pathname.match(CHAT_OR_VOICE_ROUTE);
  if (chatMatch) {
    const personaId = conversationPersonaMap.get(chatMatch[1]);
    if (personaId) {
      return chatMatch[2]
        ? { activePersonaId: null, onCallPersonaId: personaId }
        : { activePersonaId: personaId, onCallPersonaId: null };
    }
  }
  const personaMatch = pathname.match(PERSONA_PAGE_ROUTE);
  if (personaMatch && personaMatch[1] !== "new") {
    return { activePersonaId: personaMatch[1], onCallPersonaId: null };
  }
  return { activePersonaId: null, onCallPersonaId: null };
}

/** The rail's ONE ring state, plus whether a working signal is demoted to a secondary dot. */
export type PersonaPresence = "call" | "chat" | "working" | "none";

export interface PersonaPresenceResult {
  readonly presence: PersonaPresence;
  readonly secondaryWorking: boolean;
}

/**
 * Merge the three raw signals into the ONE ring + optional dot (owner
 * priority: call > in-chat > working). A working signal that loses to a
 * higher-priority state never disappears — it demotes to `secondaryWorking`
 * (rendered as a small dot, never a second ring).
 */
export function resolvePersonaPresence(input: {
  readonly isOnCall: boolean;
  readonly isActiveChat: boolean;
  readonly isWorking: boolean;
}): PersonaPresenceResult {
  if (input.isOnCall) {
    return { presence: "call", secondaryWorking: input.isWorking };
  }
  if (input.isActiveChat) {
    return { presence: "chat", secondaryWorking: input.isWorking };
  }
  if (input.isWorking) {
    return { presence: "working", secondaryWorking: false };
  }
  return { presence: "none", secondaryWorking: false };
}
