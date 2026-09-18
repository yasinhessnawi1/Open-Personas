/**
 * The Clerk Frontend API host the cloud edition proxies `/__clerk/*` to.
 *
 * Completion sweep part2 G: `next.config.ts` used to fall back to this project's own
 * production hostname when `NEXT_PUBLIC_CLERK_FRONTEND_API_HOST` was unset. A third
 * party deploying the cloud edition with their own Clerk instance then had every Clerk
 * request silently proxied to a foreign instance, baked in at build time, and nothing
 * in the repo said the knob existed. There is no reason to guess: a Clerk publishable
 * key carries its instance's Frontend API host (`pk_<live|test>_<base64(host + "$")>`,
 * the format `@clerk/shared`'s `parsePublishableKey` decodes), and every cloud build
 * already has that key. So the host is the explicit override when set, else derived
 * from the key, else absent, in which case the rewrite is simply not installed and the
 * build says so.
 */

/** The two build-time inputs the host is read from. */
export interface ClerkHostEnv {
  NEXT_PUBLIC_CLERK_FRONTEND_API_HOST?: string;
  NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY?: string;
}

const KEY_PREFIXES = new Set(["live", "test"]);
const HOST_SHAPE = /^[a-z0-9][a-z0-9.-]*[a-z0-9]$/i;

function decodeBase64(value: string): string | null {
  try {
    if (typeof Buffer !== "undefined") {
      return Buffer.from(value, "base64").toString("utf8");
    }
    return atob(value);
  } catch {
    return null;
  }
}

/**
 * The Frontend API host encoded in a Clerk publishable key, or `null` when the key is
 * missing or not a publishable key. Mirrors `parsePublishableKey` without importing
 * `@clerk/*` into the build config (a community build never pulls Clerk in).
 */
export function clerkHostFromPublishableKey(
  key: string | undefined,
): string | null {
  const parts = (key ?? "").trim().split("_");
  if (parts.length !== 3 || parts[0] !== "pk" || !KEY_PREFIXES.has(parts[1])) {
    return null;
  }
  const decoded = decodeBase64(parts[2]);
  if (!decoded || !decoded.endsWith("$")) {
    return null;
  }
  const host = decoded.slice(0, -1);
  return HOST_SHAPE.test(host) ? host : null;
}

/**
 * The host to rewrite `/__clerk/*` to: the explicit override when set, else the host
 * the publishable key names, else `null` (no rewrite; never someone else's instance).
 */
export function clerkFrontendApiHost(env: ClerkHostEnv): string | null {
  const explicit = env.NEXT_PUBLIC_CLERK_FRONTEND_API_HOST?.trim();
  if (explicit) {
    return explicit;
  }
  return clerkHostFromPublishableKey(env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY);
}
