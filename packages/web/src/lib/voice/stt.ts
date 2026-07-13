/**
 * R9-025a — mic dictation client: `POST /v1/stt` (persona-api).
 *
 * Owner-scoped only, no persona (dictation works before a persona exists —
 * the author wizard's description field). Mirrors `tts.ts`'s "web talks to
 * the api only" proxy shape: audio bytes go up as multipart, a JSON
 * `{transcript}` comes back.
 */

import {
  ApiError,
  type ApiErrorBody,
  readRateLimit,
  type TokenGetter,
} from "@/lib/api/client";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export interface TranscribeAudioOptions {
  getToken: TokenGetter;
  signal?: AbortSignal;
  /**
   * Optional ISO-639-1-ish context hint (R9-025 reopen — context-pinned
   * dictation language): the conversation persona's declared
   * `identity.language_default` (chat composer), or the active UI locale
   * (persona-authoring mic). Forwarded verbatim as a `language` form field
   * when non-empty; omitted entirely otherwise, which keeps the api
   * proxy/voice service on the 7647699 auto-detect fallback. Deepgram's
   * `detect_language` has limited/uneven coverage, so a caller that KNOWS
   * the language should always pin it rather than rely on detection.
   */
  language?: string;
}

/**
 * Transcribe a recorded audio clip. Resolves with the transcript text
 * (``""`` for a silent/empty clip — not an error); throws {@link ApiError}
 * on a non-2xx — including 503 `voice_unavailable` (fail-soft
 * feature-absent — hide the mic affordance) and 413 when the audio exceeds
 * the transcription size limit.
 */
export async function transcribeAudio(
  audio: Blob,
  options: TranscribeAudioOptions,
): Promise<string> {
  const jwt = await options.getToken();
  const form = new FormData();
  form.append("audio", audio, "dictation.webm");
  if (options.language) form.append("language", options.language);
  const response = await fetch(`${API_BASE_URL}/v1/stt`, {
    method: "POST",
    // NOTE: no Content-Type header — the browser sets the multipart
    // boundary itself; setting it manually would break the upload.
    headers: jwt ? { Authorization: `Bearer ${jwt}` } : {},
    body: form,
    signal: options.signal,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => undefined)) as
      | ApiErrorBody
      | undefined;
    throw new ApiError(response.status, body, readRateLimit(response.headers));
  }
  const data = (await response.json()) as { transcript?: string };
  return data.transcript ?? "";
}
