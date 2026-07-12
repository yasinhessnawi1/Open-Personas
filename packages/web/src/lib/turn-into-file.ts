/**
 * R9-025b — "Turn into file" message action.
 *
 * Thin wrapper around
 * `POST /v1/conversations/:id/messages/:message_id/turn-into-file`. Fires the
 * durable extraction job and returns immediately (202 + a job reference) —
 * the file itself lands later, out of band; the chat Files surface picks it
 * up on its own refresh/poll (see the api's `file_extract` module docstring
 * for the exact refresh-signal decision).
 */

import type { TokenGetter } from "@/lib/api/client";
import { createApiClient, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type TurnIntoFileFormat =
  components["schemas"]["TurnIntoFileRequest"]["format"];

export type TurnIntoFileResult = components["schemas"]["TurnIntoFileResponse"];

export async function turnMessageIntoFile(
  conversationId: string,
  messageId: string,
  format: TurnIntoFileFormat,
  getToken: TokenGetter,
): Promise<TurnIntoFileResult> {
  const jwt = await getToken();
  const client = createApiClient(() => Promise.resolve(jwt));
  const result = await client.POST(
    "/v1/conversations/{conversation_id}/messages/{message_id}/turn-into-file",
    {
      params: {
        path: { conversation_id: conversationId, message_id: messageId },
      },
      body: { format },
    },
  );
  return unwrap(result);
}
