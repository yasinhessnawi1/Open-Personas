/**
 * Spec N7-T3b — the MCP OAuth callback page.
 *
 * Verifies the token-flow contract at the web boundary: the page relays `code` +
 * `state` to POST /v1/mcp-servers/oauth/callback with the user's Bearer JWT, then
 * routes to `redirect_after` on success; the single-use-state once-guard fires the
 * exchange exactly once under React strict-mode double-effect; a failure shows the
 * honest error card, with a sign-in hint specifically on a 401.
 */

import { render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import McpOauthCallbackPage from "./page";

const replace = vi.fn();
let searchString = "code=CODE_1&state=STATE_1";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => new URLSearchParams(searchString),
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("jwt") }),
}));

interface Captured {
  url: string;
  method: string;
  body: string;
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(responder: (url: string, method: string) => Response): {
  captured: Captured[];
  restore: () => void;
} {
  const captured: Captured[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = vi.fn(
    async (input: string | URL | Request, init?: RequestInit) => {
      const isReq = typeof input === "object" && "method" in input;
      const req = isReq ? (input as Request) : null;
      const url = req ? req.url : input.toString();
      const method = init?.method ?? req?.method ?? "GET";
      let body = typeof init?.body === "string" ? init.body : "";
      if (!body && req) body = await req.clone().text();
      captured.push({ url, method, body });
      return responder(url, method);
    },
  ) as unknown as typeof fetch;
  return {
    captured,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function renderPage() {
  return render(
    <StrictMode>
      <NextIntlClientProvider locale="en" messages={messages}>
        <McpOauthCallbackPage />
      </NextIntlClientProvider>
    </StrictMode>,
  );
}

describe("McpOauthCallbackPage (N7-T3b)", () => {
  let restore: () => void;
  afterEach(() => {
    restore?.();
    replace.mockClear();
    searchString = "code=CODE_1&state=STATE_1";
  });

  it("posts code+state once (strict-mode guard) and routes to redirect_after", async () => {
    const { captured, restore: r } = installFetch(() =>
      jsonResponse({
        server: { id: "srv_1" },
        redirect_after: "/personas/p1/edit",
      }),
    );
    restore = r;
    renderPage();

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/personas/p1/edit"),
    );
    const posts = captured.filter(
      (c) =>
        c.method === "POST" && c.url.includes("/v1/mcp-servers/oauth/callback"),
    );
    // Single-use state: exactly one exchange even though strict mode double-mounts.
    expect(posts.length).toBe(1);
    expect(JSON.parse(posts[0].body)).toEqual({
      code: "CODE_1",
      state: "STATE_1",
    });
  });

  it("defaults to /personas when no redirect_after is returned", async () => {
    const { restore: r } = installFetch(() =>
      jsonResponse({ server: { id: "srv_1" }, redirect_after: null }),
    );
    restore = r;
    renderPage();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/personas"));
  });

  // R9-048: a belt-and-suspenders client guard — even if a malicious/stale
  // redirect_after somehow reached the browser, router.replace() must never
  // be handed an external target. Falls back to the safe default instead.
  it.each([
    "https://evil.com",
    "http://evil.com",
    "//evil.com",
    "javascript:alert(1)",
    "/\\evil.com",
  ])(
    "falls back to /personas for a malicious redirect_after: %s",
    async (maliciousRedirect) => {
      const { restore: r } = installFetch(() =>
        jsonResponse({
          server: { id: "srv_1" },
          redirect_after: maliciousRedirect,
        }),
      );
      restore = r;
      renderPage();
      await waitFor(() => expect(replace).toHaveBeenCalledWith("/personas"));
      expect(replace).not.toHaveBeenCalledWith(maliciousRedirect);
    },
  );

  it("shows the error card on failure and never routes", async () => {
    const { restore: r } = installFetch(() =>
      jsonResponse({ error: "bad_state" }, 400),
    );
    restore = r;
    const { container } = renderPage();
    await waitFor(() =>
      expect(
        container.querySelector('[data-slot="mcp-oauth-error"]'),
      ).toBeTruthy(),
    );
    expect(replace).not.toHaveBeenCalled();
    // Generic (non-401) error copy, not the sign-in hint.
    expect(container.textContent).toContain(
      messages.mcpOauth.errorBody.slice(0, 20),
    );
  });

  it("shows the sign-in hint specifically on a 401", async () => {
    const { restore: r } = installFetch(() =>
      jsonResponse({ error: "unauthorized" }, 401),
    );
    restore = r;
    const { container } = renderPage();
    await waitFor(() =>
      expect(
        container.querySelector('[data-slot="mcp-oauth-error"]'),
      ).toBeTruthy(),
    );
    expect(container.textContent).toContain(
      messages.mcpOauth.errorSignIn.slice(0, 20),
    );
  });
});
