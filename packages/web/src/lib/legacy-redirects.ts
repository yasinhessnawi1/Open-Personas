/**
 * Spec R11 (B1) — legacy route redirects for the v3 information architecture.
 *
 * D-R11-4: the Activity area consolidates under `/activity` (Review · Tasks ·
 * Approvals + task detail); the old standalone routes redirect so deep links
 * keep working. D-R11-3: Connectors is promoted from a settings subsection to
 * a first-class `/connectors` tab.
 *
 * Consumed by `next.config.ts` `redirects()`. Query strings are forwarded
 * automatically by Next, which two flows depend on:
 *   - `/approvals?id=…` (the A6-D-6 per-item deep link from digests/toasts)
 *   - `/settings/connectors?result=…` (the C6 OAuth 302-return; the redirect
 *     URI is registered with external providers, so the old path must keep
 *     resolving indefinitely)
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
  {
    source: "/settings/connectors",
    destination: "/connectors",
    permanent: false,
  },
] as const;
