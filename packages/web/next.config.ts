import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

import { clerkFrontendApiHost } from "./src/lib/clerk-frontend-api-host";
import { LEGACY_REDIRECTS } from "./src/lib/legacy-redirects";

// Spec 33 (D-33-2): the auth layer is edition-selected at BUILD time. Build is
// Turbopack (Next 16), so the swap is `turbopack.resolveAlias` (a webpack alias
// would break `next build`). `@/auth*` resolves to the edition's variant; a
// `community` build never pulls `@clerk/*` into the module graph. Default is
// `community` (the product default, D-33-1); the cloud deploy sets
// `PERSONA_EDITION=cloud`. The tsconfig `paths` point at the cloud variants so
// the type-checker / tests resolve `@/auth` to the richer Clerk surface.
//
// The alias TARGET is the `@/auth/<variant>` specifier (not an absolute or `./`
// filesystem path): Turbopack 16.2 rejects an absolute resolveAlias target as a
// "server relative import (not implemented yet)" and does not resolve a bare
// relative `./src/...` target from the project root. Routing through the `@/*`
// tsconfig path lets Turbopack's own resolver land on the variant file.
const EDITION = process.env.PERSONA_EDITION === "cloud" ? "cloud" : "community";
const authResolveAlias: Record<string, string> = {
  "@/auth": `@/auth/index.${EDITION}`,
  "@/auth/server": `@/auth/server.${EDITION}`,
  "@/auth/provider": `@/auth/provider.${EDITION}`,
  "@/auth/middleware": `@/auth/middleware.${EDITION}`,
};

// Clerk Production Frontend API host. When the production instance is set up
// with a custom Application Domain + Allowed Subdomains (so cookies stay
// first-party on `app.<root>`), Clerk's client tries to load its JS bundle and
// reach its API via `<current-host>/__clerk/*`. Without an edge rewrite, those
// paths 404 on Vercel. We rewrite them server-side to Clerk's actual CDN.
//
// The host is `NEXT_PUBLIC_CLERK_FRONTEND_API_HOST` when set, else the host the
// publishable key itself names (completion sweep part2 G: the old fallback was this
// project's production hostname, which silently pointed any third party's cloud build
// at a foreign Clerk instance). No host means no rewrite, and the build says so.
const CLERK_FRONTEND_API_HOST = clerkFrontendApiHost({
  NEXT_PUBLIC_CLERK_FRONTEND_API_HOST:
    process.env.NEXT_PUBLIC_CLERK_FRONTEND_API_HOST,
  NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY:
    process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY,
});
if (EDITION === "cloud" && CLERK_FRONTEND_API_HOST === null) {
  console.warn(
    "[next.config] cloud edition without a Clerk Frontend API host: set " +
      "NEXT_PUBLIC_CLERK_FRONTEND_API_HOST or a valid NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY; " +
      "the /__clerk/* rewrite is not installed for this build",
  );
}

const nextConfig: NextConfig = {
  turbopack: { resolveAlias: authResolveAlias },
  // Spec R11 (B1): the v3 IA re-homes — /activity consolidation + the
  // /connectors tab. Table + rationale live in src/lib/legacy-redirects.ts.
  async redirects() {
    return [...LEGACY_REDIRECTS];
  },
  // The Clerk auto-proxy rewrite is cloud-only — community has no Clerk.
  ...(EDITION === "cloud" && CLERK_FRONTEND_API_HOST !== null
    ? {
        async rewrites() {
          return [
            {
              source: "/__clerk/:path*",
              destination: `https://${CLERK_FRONTEND_API_HOST}/:path*`,
            },
          ];
        },
      }
    : {}),
};

// Routes every request through next-intl's request config (src/i18n/request.ts).
const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

export default withNextIntl(nextConfig);
