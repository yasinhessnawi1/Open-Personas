import { ApiError, type ApiErrorBody, readRateLimit } from "./api/client";

/**
 * SSE consumption (D-09-1). Uses `fetch` + `ReadableStream`, NOT `EventSource`
 * — the chat endpoint is a POST with an `Authorization: Bearer` header, which
 * EventSource cannot send. Yields raw `{event, data}` frames; typed parsing lives
 * in sse-types.ts (`parseChatEvent` / `parseRunEvent`) because the two streams
 * use different envelopes.
 */

/**
 * One parsed SSE frame: the `event:` name (default "message") + raw `data:` string
 * + the optional `id:` (the `Last-Event-ID` cursor — surfaced for A11's persistent
 * `/v1/me/events` channel, which resumes by it; the chat/run streams ignore it).
 */
export interface RawSSEEvent {
  event: string;
  data: string;
  id?: string;
}

function parseFrame(frame: string): RawSSEEvent | null {
  let event = "message";
  let id: string | undefined;
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // blank / comment
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1); // SSE strips one leading space
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
    else if (field === "id") id = value;
    // retry ignored — not used by any stream
  }
  if (dataLines.length === 0) return null;
  return {
    event,
    data: dataLines.join("\n"),
    ...(id !== undefined ? { id } : {}),
  };
}

/**
 * Stream SSE frames from `url`. Pass `method`/`headers`/`body`/`signal` via
 * `init` (the auth header + JWT are the caller's responsibility — T03). Throws
 * {@link ApiError} if the response is not OK (e.g. 429 before streaming starts),
 * carrying the structured body + rate-limit headers. Aborting `init.signal`
 * stops the stream (the generator throws AbortError — catch it at the call site).
 */
export async function* consumeSSE(
  url: string,
  init: RequestInit = {},
): AsyncGenerator<RawSSEEvent> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: "text/event-stream", ...init.headers },
  });

  if (!response.ok) {
    const body = (await response.json().catch(() => undefined)) as
      | ApiErrorBody
      | undefined;
    throw new ApiError(response.status, body, readRateLimit(response.headers));
  }
  if (!response.body) {
    throw new ApiError(
      response.status,
      { error: "no_response_body" },
      readRateLimit(response.headers),
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let sep = buffer.indexOf("\n\n");
      while (sep !== -1) {
        const frame = parseFrame(buffer.slice(0, sep));
        buffer = buffer.slice(sep + 2);
        if (frame) yield frame;
        sep = buffer.indexOf("\n\n");
      }
    }
    // Flush a trailing frame that wasn't terminated by a blank line.
    const tail = parseFrame(buffer);
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}
