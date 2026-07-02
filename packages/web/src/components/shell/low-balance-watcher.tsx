"use client";

/**
 * Spec P6 (P6-D-6) — low-balance-at-load flag.
 *
 * A headless island mounted once in the app shell (inside NotificationProvider).
 * On load it reads `GET /v1/me/credits` and, if the caller is under the
 * server-computed `low_balance` threshold *and* still has a positive balance,
 * emits a persistent low-priority notification through `useNotify()` — deep-
 * linking to billing (`/settings`) — **at most once per session**.
 *
 * Design (per the decisions):
 *   - Reuses the server `low_balance` flag (single source of truth, threshold in
 *     `credits/service.py`); no web-side threshold to drift (P6-D-6).
 *   - Zero balance is the hard 402 cliff, a separate surface — we only warn while
 *     `balance > 0` (the "you're about to run out" moment).
 *   - Once per session: a `sessionStorage` guard survives in-app navigation and
 *     reloads within the session, so the user isn't re-nagged; the notification
 *     is client-only and does NOT persist to the durable cross-device feed.
 *   - Best-effort: a failed/absent read never disrupts load (no throw into the
 *     render tree).
 */

import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import { useAuth } from "@/auth";
import { useNotify } from "@/components/providers/notification-provider";
import { createApiClient, unwrap } from "@/lib/api/client";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;
/** Guard key: once set, we don't re-check or re-nag for the rest of the session. */
const SESSION_KEY = "open-persona:low-balance-notified";
/** Billing lives on the settings page (the low-balance card + credit balance). */
const BILLING_HREF = "/settings";

export function LowBalanceWatcher() {
  const { getToken } = useAuth();
  const { notify } = useNotify();
  const t = useTranslations("notifications");

  // Read auth + notify through refs so the effect can run exactly once without
  // depending on identities a non-memoising host might churn each render.
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const tRef = useRef(t);
  tRef.current = t;
  const firedRef = useRef(false);

  useEffect(() => {
    // Same-mount guard (StrictMode double-invoke) + cross-mount/reload guard.
    if (firedRef.current) return;
    if (
      typeof window !== "undefined" &&
      window.sessionStorage.getItem(SESSION_KEY)
    ) {
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const jwt = await getTokenRef.current(
          TEMPLATE ? { template: TEMPLATE } : undefined,
        );
        const client = createApiClient(() => Promise.resolve(jwt));
        const credits = await unwrap(await client.GET("/v1/me/credits"));
        if (cancelled) return;
        if (credits.low_balance && credits.balance > 0) {
          firedRef.current = true;
          if (typeof window !== "undefined") {
            window.sessionStorage.setItem(SESSION_KEY, "1");
          }
          notifyRef.current({
            level: "warning",
            persist: true,
            title: tRef.current("lowBalance.title"),
            body: tRef.current("lowBalance.body", { count: credits.balance }),
            href: BILLING_HREF,
          });
        }
      } catch {
        // Best-effort — a failed read (401 during a session race, offline, etc.)
        // must never disrupt load. The next hard load re-checks.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return null;
}
