"use client";

import { AlertTriangle, Loader2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Suspense, useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";
import { buttonVariants } from "@/components/ui/button";
import { ApiError, createApiClient, unwrap } from "@/lib/api/client";
import { cn } from "@/lib/utils";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/** The safe fallback when `redirect_after` is missing or fails the guard below. */
const SAFE_DEFAULT_REDIRECT = "/personas";

/**
 * R9-048 (defense-in-depth): the API validates `redirect_after` is a relative
 * in-app path at the request boundary, but this page still hands whatever the
 * callback response returns straight to `router.replace()` — a belt-and-
 * suspenders client guard so a future/compromised API response, or a stale
 * client talking to an older server without the server-side check, can never
 * plant an external redirect here. Mirrors the server validator: a single
 * leading `/` (never `//`, never a scheme, never a backslash — browsers
 * normalize `\` to `/`, turning `/\evil.com` into the protocol-relative form).
 */
function isSafeRedirectPath(path: string): boolean {
  if (!path.startsWith("/") || path.startsWith("//")) return false;
  if (path.includes("\\")) return false;
  for (let i = 0; i < path.length; i++) {
    const code = path.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return false;
  }
  return true;
}

/**
 * Spec N7-T3b — the MCP OAuth callback page (closes R8-D-8's orphaned web half).
 *
 * The provider (GitHub / an mcp-native AS) redirects the user's browser here with
 * `?code=…&state=…` after they authorize. This page relays that pair to
 * `POST /v1/mcp-servers/oauth/callback` with the user's Bearer JWT — the API
 * consumes the single-use state (RLS-scoped, CSRF/owner/expiry checked), exchanges
 * the code on the back channel, and persists the tokens encrypted. NO secret ever
 * touches the client: the code is exchanged server-side and the token is never
 * returned.
 *
 * Routing (verified at implementation): this page lives OUTSIDE the (app) route
 * group and its path `/mcp/oauth/callback` is NOT in the cloud middleware's
 * `isProtected` matcher (personas|chat|runs|conversations|settings) — so the OAuth
 * redirect lands WITHOUT a sign-in bounce that would drop the query params. The
 * root layout still provides AuthProvider (so `useAuth().getToken` works) + i18n +
 * theme. Community middleware is a passthrough (no-auth by design).
 *
 * Single-use-state discipline: React strict-mode double-mounts effects in dev, and
 * the second callback POST would 400 (the state is consumed once). A `useRef`
 * once-guard fires the exchange exactly once.
 */
function OAuthCallback() {
  const t = useTranslations("mcpOauth");
  const router = useRouter();
  const params = useSearchParams();
  const { getToken } = useAuth();
  const [phase, setPhase] = useState<"connecting" | "error">("connecting");
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const ran = useRef(false);

  const code = params.get("code");
  const state = params.get("state");

  useEffect(() => {
    // The OAuth state is single-use: a second exchange (strict-mode double-effect)
    // would 400. Guard to exactly one POST.
    if (ran.current) return;
    ran.current = true;

    void (async () => {
      if (!code || !state) {
        setPhase("error");
        return;
      }
      try {
        const jwt = await getToken(
          TEMPLATE ? { template: TEMPLATE } : undefined,
        );
        const api = createApiClient(() => Promise.resolve(jwt));
        const res = await unwrap(
          await api.POST("/v1/mcp-servers/oauth/callback", {
            body: { code, state },
          }),
        );
        // Back to where the Connect started (or personas as a safe default).
        // R9-048: never trust redirect_after verbatim — the client guard is
        // the belt to the server's suspenders (see isSafeRedirectPath above).
        const target = res.redirect_after;
        router.replace(
          target && isSafeRedirectPath(target) ? target : SAFE_DEFAULT_REDIRECT,
        );
      } catch (e) {
        // A 401 means no signed-in cloud session carried into the callback —
        // give the user the honest "sign in and retry" hint.
        if (e instanceof ApiError && e.status === 401) setNeedsSignIn(true);
        setPhase("error");
      }
    })();
  }, [code, state, getToken, router]);

  if (phase === "connecting") {
    return (
      <main
        className="mx-auto flex min-h-[60vh] max-w-md flex-col items-center justify-center gap-3 px-4 text-center"
        data-slot="mcp-oauth-connecting"
      >
        <Loader2
          className="size-6 animate-spin text-muted-foreground"
          aria-hidden="true"
        />
        <p className="text-sm text-muted-foreground">{t("connecting")}</p>
      </main>
    );
  }

  return (
    <main
      className="mx-auto flex min-h-[60vh] max-w-md flex-col items-center justify-center gap-3 px-4 text-center"
      data-slot="mcp-oauth-error"
    >
      <div className="flex flex-col items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-6">
        <AlertTriangle className="size-6 text-destructive" aria-hidden="true" />
        <h1 className="font-heading text-base font-semibold text-foreground">
          {t("errorHeading")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {needsSignIn ? t("errorSignIn") : t("errorBody")}
        </p>
        <Link
          href="/personas"
          className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
          data-slot="mcp-oauth-back"
        >
          {t("back")}
        </Link>
      </div>
    </main>
  );
}

export default function McpOauthCallbackPage() {
  // useSearchParams requires a Suspense boundary (Next App Router CSR bailout).
  return (
    <Suspense
      fallback={
        <main className="mx-auto flex min-h-[60vh] max-w-md items-center justify-center px-4">
          <Loader2
            className="size-6 animate-spin text-muted-foreground"
            aria-hidden="true"
          />
        </main>
      }
    >
      <OAuthCallback />
    </Suspense>
  );
}
