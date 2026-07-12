/**
 * R9-025a — read-aloud client: `POST /v1/personas/:id/tts` (persona-api).
 *
 * Distinct from `voices.ts`/`token.ts`: those call persona-voice DIRECTLY
 * (`VOICE_BASE_URL`) for the realtime call surface. Read-aloud + mic
 * dictation go through persona-api's thin proxy instead (the R9-025 RESCOPE
 * decision — "Web talks to the api only"), which resolves the persona's
 * configured voice server-side and fails soft (503 `voice_unavailable`)
 * when the deployment has no voice service configured. Mirrors `voices.ts`'s
 * Bearer + ApiError discipline; the response body is audio bytes, not JSON.
 */

import {
  ApiError,
  type ApiErrorBody,
  readRateLimit,
  type TokenGetter,
} from "@/lib/api/client";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export interface FetchPersonaTtsOptions {
  getToken: TokenGetter;
  signal?: AbortSignal;
}

/**
 * Synthesise `text` in `personaId`'s configured voice. Resolves with the
 * audio {@link Blob} (WAV); throws {@link ApiError} on a non-2xx — including
 * 503 `voice_unavailable` (the deployment has no voice service configured,
 * OR this persona has none), which the caller treats as fail-soft
 * feature-absent (hide the read-aloud affordance), and 413 when `text`
 * exceeds the synthesis size limit.
 */
export async function fetchPersonaTts(
  personaId: string,
  text: string,
  options: FetchPersonaTtsOptions,
): Promise<Blob> {
  const jwt = await options.getToken();
  const response = await fetch(
    `${API_BASE_URL}/v1/personas/${encodeURIComponent(personaId)}/tts`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(jwt ? { Authorization: `Bearer ${jwt}` } : {}),
      },
      body: JSON.stringify({ text }),
      signal: options.signal,
    },
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => undefined)) as
      | ApiErrorBody
      | undefined;
    throw new ApiError(response.status, body, readRateLimit(response.headers));
  }
  return await response.blob();
}
