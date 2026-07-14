"use server";

import { redirect } from "next/navigation";
import { serverApi } from "@/lib/api/server";
import { readPreferredModel, yamlToDoc } from "@/lib/persona-draft";

interface PydanticError {
  msg?: string;
}

function formatDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as PydanticError;
    if (typeof first.msg === "string") return first.msg;
  }
  return fallback;
}

/**
 * Persist an edited persona YAML (PATCH, re-validated server-side) and redirect
 * to its detail page (T08). Returns a structured error on validation failure so
 * the editor can surface it instead of crashing (redirect only on success).
 *
 * ``avatarUrl`` (a workspace ref from an avatar upload) rides the same PATCH; the
 * API leaves the avatar untouched when it is null/omitted, so an unchanged avatar
 * is preserved.
 */
export async function savePersona(
  personaId: string,
  yaml: string,
  avatarUrl?: string | null,
): Promise<{ error: string } | undefined> {
  const api = await serverApi();
  const res = await api.PATCH("/v1/personas/{persona_id}", {
    params: { path: { persona_id: personaId } },
    body: { yaml, avatar_url: avatarUrl ?? null },
  });
  if (res.error !== undefined) {
    const body = res.error as { error?: string; detail?: unknown };
    return {
      error: formatDetail(body.detail, body.error ?? "save_failed"),
    };
  }
  redirect(`/personas/${personaId}`);
}

/**
 * R11-B6 — the consolidated persona page's AUTOSAVE door: the same PATCH as
 * `savePersona`, WITHOUT the redirect (the user is already on the page; a
 * debounced field edit must never navigate). Returns the structured error for
 * the status bar.
 */
export async function savePersonaInline(
  personaId: string,
  yaml: string,
  avatarUrl?: string | null,
): Promise<{ error: string } | undefined> {
  const api = await serverApi();
  const res = await api.PATCH("/v1/personas/{persona_id}", {
    params: { path: { persona_id: personaId } },
    body: { yaml, avatar_url: avatarUrl ?? null },
  });
  if (res.error !== undefined) {
    const body = res.error as { error?: string; detail?: unknown };
    return {
      error: formatDetail(body.detail, body.error ?? "save_failed"),
    };
  }
  return undefined;
}

/**
 * Set a persona's auto-dispatch consent (Spec 21 T09 / Spec 31 T6). Tri-state:
 * ``true`` = grant, ``false`` = decline, ``null`` = revoke back to "ask". An
 * inline settings toggle (no redirect); returns a structured error on failure so
 * the editor can revert its optimistic state.
 */
export async function setConsent(
  personaId: string,
  granted: boolean | null,
): Promise<{ error: string } | undefined> {
  const api = await serverApi();
  const res = await api.PATCH("/v1/personas/{persona_id}/consent", {
    params: { path: { persona_id: personaId } },
    body: { granted },
  });
  if (res.error !== undefined) {
    const body = res.error as { error?: string; detail?: unknown };
    return {
      error: formatDetail(body.detail, body.error ?? "consent_failed"),
    };
  }
  return undefined;
}

/**
 * Create a persona from a reviewed authoring draft (spec 10, D-10-2). Authoring
 * now returns a draft (no row); the user saves the reviewed YAML here, which
 * creates the persona and redirects to its detail page. Validation errors are
 * returned structured (redirect only on success).
 */
export async function createPersona(
  yaml: string,
): Promise<{ error: string } | undefined> {
  const api = await serverApi();
  const res = await api.POST("/v1/personas", { body: { yaml } });
  if (res.error !== undefined) {
    const body = res.error as { error?: string; detail?: unknown };
    return {
      error: formatDetail(body.detail, body.error ?? "save_failed"),
    };
  }
  // Spec M1 (M1-T7): a model the user actively picked for THIS persona becomes
  // their sticky per-user default for next time (T6's `preferred_model`).
  // Best-effort / fire-and-forget: a hiccup here must never fail or delay the
  // persona that was just successfully created. Awaited (not a detached
  // promise) because `redirect()` below throws and unwinds the call stack —
  // any code placed after it would never run, so this has to happen first.
  await stickPreferredModel(api, yaml);
  redirect(`/personas/${res.data.id}`);
}

/**
 * Best-effort PATCH of the caller's sticky model default; swallows all errors.
 *
 * Always PATCHes, with the persona's chosen model id OR explicit `null` when
 * the user left it on the tier default (design §9). A `null` is sent as an
 * explicit JSON `null`, not omitted — the API treats that as a CLEAR (pinned
 * by `test_patch_null_clears_preferred_model`) — so creating with the tier
 * default selected resets a previously-stuck choice back to clean, rather
 * than leaving a stale prior pick sticky forever.
 */
async function stickPreferredModel(
  api: Awaited<ReturnType<typeof serverApi>>,
  yaml: string,
): Promise<void> {
  try {
    const model = readPreferredModel(yamlToDoc(yaml));
    await api.PATCH("/v1/me/profile", { body: { preferred_model: model } });
  } catch {
    // never blocks/fails persona creation
  }
}
