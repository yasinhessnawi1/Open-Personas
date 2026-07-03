/**
 * Spec C6 (T7) — the OAuth `?result=` return decision (C6-D-2).
 *
 * `?result=` is TOAST-ONLY *voice* — the connected STATE is always the list (the cards),
 * never the query param. Even the toast keys off the list: a claimed `connected` with NO
 * binding present (forged / stale / a callback race) resolves to an honest "unconfirmed",
 * never a success. So a forged `?result=connected` can never make anything read as connected.
 */
export type ReturnToastKind =
  | "success"
  | "unconfirmed"
  | "denied"
  | "expired"
  | "failed";

export function resolveReturnToast(
  result: string | null,
  platformConnected: boolean,
): ReturnToastKind | null {
  switch (result) {
    case "connected":
      // The list is the oracle: only a real binding earns a success voice.
      return platformConnected ? "success" : "unconfirmed";
    case "denied":
      return "denied";
    case "expired":
      return "expired";
    case "failed":
      return "failed";
    default:
      // Unknown / absent → no voice (and nothing rendered as connected).
      return null;
  }
}
