/**
 * Spec R11 (B1) — legacy route redirects for the v3 information architecture.
 *
 * D-R11-4: the Activity area consolidates under `/activity` (Review · Tasks ·
 * Approvals + task detail); the old standalone routes redirect so deep links
 * keep working. (Connectors stays at `/settings/connectors` — the D-R11-3
 * promotion was owner-reversed: one-time setup lives with settings.)
 *
 * Consumed by `next.config.ts` `redirects()`. Query strings are forwarded
 * automatically by Next, which the A6-D-6 approval deep link
 * (`/approvals?id=…` from digests/toasts) depends on.
 *
 * `permanent: false` (307) deliberately: browsers cache 308s aggressively, and
 * these targets may still be re-homed within the v3 adoption wave.
 */
export const LEGACY_REDIRECTS = [
  { source: "/review", destination: "/activity", permanent: false },
  { source: "/tasks", destination: "/activity/tasks", permanent: false },
  {
    source: "/tasks/:taskId",
    destination: "/activity/tasks/:taskId",
    permanent: false,
  },
  {
    source: "/approvals",
    destination: "/activity/approvals",
    permanent: false,
  },
] as const;
