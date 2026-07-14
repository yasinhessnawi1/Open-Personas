/**
 * Spec N4 (Group D) — the credential-isolated app setup form.
 *
 * Mocks fetch (openapi-fetch hits global fetch) to assert the form posts the
 * credential STRAIGHT to POST /v1/personas/{id}/adopted-apps (never through a
 * persona turn), carrying { catalog_name, credential }, and that the credential
 * appears only in that request body — then shows the connected state. A 409
 * surfaces the "already set up" message, not a 500.
 */

import { fireEvent, render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { AppSetupForm } from "./app-setup-form";
import type { McpCatalogEntry } from "./persona-form";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("jwt") }),
}));

const _SECRET = "user-pasted-token-do-not-leak";

const _APP: McpCatalogEntry = {
  name: "notion-remote",
  description: "Hosted Notion app.",
  provider: "mcp:optional",
  defaultEnabled: false,
  requiredEnv: ["NOTION_TOKEN"],
  displayName: "Notion",
  iconUrl: "",
  image: "",
  serverType: "remote",
  risk: "medium",
  sourceProject: "",
  sourceCommit: "",
  signed: false,
  allowHosts: ["mcp.notion.com:443"],
  secrets: [
    {
      name: "notion.token",
      env: "NOTION_TOKEN",
      example: "ntn_xxx",
      description: "Create an integration token at notion.so/my-integrations.",
    },
  ],
  authMethod: "",
  oauthProvider: "",
};

interface Captured {
  url: string;
  method: string;
  body: string;
}

function installFetch(status = 201): {
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
      if (method === "POST" && url.includes("/adopted-apps")) {
        if (status >= 400) {
          return new Response(
            JSON.stringify({
              error: { type: "mcp_app_already_adopted", message: "x" },
            }),
            { status, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            id: "srv_1",
            name: "notion-remote",
            url: "https://mcp.notion.com/mcp",
            auth_method: "bearer",
            enabled: true,
            has_credential: true,
            catalog_source: "notion-remote",
            discovered_tools: null,
            created_at: "2026-06-30T00:00:00Z",
            updated_at: "2026-06-30T00:00:00Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(null, { status: 204 });
    },
  ) as unknown as typeof fetch;
  return {
    captured,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function renderForm() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <AppSetupForm app={_APP} personaId="p1" />
    </NextIntlClientProvider>,
  );
}

// Spec N7 (D-N7-2, owner-ratified) — a SECRETLESS image app (e.g. a runtime-only
// app needing no credential): the accommodation renders no input and submits
// immediately with `credential: null`.
const _SECRETLESS_APP: McpCatalogEntry = {
  ..._APP,
  name: "secretless-image",
  displayName: "Secretless Image App",
  serverType: "server",
  secrets: [],
  requiredEnv: [],
};

function renderSecretlessForm() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <AppSetupForm app={_SECRETLESS_APP} personaId="p1" />
    </NextIntlClientProvider>,
  );
}

describe("AppSetupForm (Spec N4 Group D)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  it("renders the schema-driven credential field + how-to-obtain help", () => {
    const { restore: r } = installFetch();
    restore = r;
    const { container, getByText } = renderForm();
    expect(
      container.querySelector('[data-slot="app-setup-form"]'),
    ).toBeTruthy();
    expect(
      container.querySelector('[data-slot="app-setup-credential"]'),
    ).toBeTruthy();
    // the declared secret's description drives the how-to-obtain help line.
    expect(getByText(/notion\.so\/my-integrations/)).toBeTruthy();
  });

  it("posts the credential straight to the adopt route, then shows the connected state", async () => {
    const { captured, restore: r } = installFetch();
    restore = r;
    const { container } = renderForm();

    const input = container.querySelector(
      '[data-slot="app-setup-credential"]',
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: _SECRET } });
    (
      container.querySelector(
        '[data-slot="app-setup-submit"]',
      ) as HTMLButtonElement
    ).click();

    await waitFor(() => {
      const post = captured.find(
        (c) =>
          c.method === "POST" && c.url.includes("/v1/personas/p1/adopted-apps"),
      );
      expect(post).toBeTruthy();
      const parsed = JSON.parse(post?.body ?? "{}");
      expect(parsed.catalog_name).toBe("notion-remote");
      expect(parsed.credential).toBe(_SECRET);
    });

    // the credential appears ONLY in the adopt POST body — in no other request.
    const leaks = captured.filter(
      (c) => !c.url.includes("/adopted-apps") && c.body.includes(_SECRET),
    );
    expect(leaks).toEqual([]);

    // success → the connected state replaces the form.
    await waitFor(() =>
      expect(
        container.querySelector('[data-slot="app-setup-done"]'),
      ).toBeTruthy(),
    );
  });

  it("surfaces a 409 as a clear 'already set up' message, not a crash", async () => {
    const { restore: r } = installFetch(409);
    restore = r;
    const { container, findByText } = renderForm();
    const input = container.querySelector(
      '[data-slot="app-setup-credential"]',
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: _SECRET } });
    (
      container.querySelector(
        '[data-slot="app-setup-submit"]',
      ) as HTMLButtonElement
    ).click();

    expect(await findByText(/already set up/i)).toBeTruthy();
    expect(container.querySelector('[data-slot="app-setup-done"]')).toBeNull();
  });

  // Spec N7 (D-N7-2) — the secretless AppSetupForm accommodation.
  it("secretless app: no credential input, submit enabled immediately, posts credential: null", async () => {
    const { captured, restore: r } = installFetch();
    restore = r;
    const { container } = renderSecretlessForm();

    // No input rendered — there is nothing to type.
    expect(
      container.querySelector('[data-slot="app-setup-credential"]'),
    ).toBeNull();

    const submit = container.querySelector(
      '[data-slot="app-setup-submit"]',
    ) as HTMLButtonElement;
    // Enabled immediately — never gated on a credential that will never exist.
    expect(submit.disabled).toBe(false);
    submit.click();

    await waitFor(() => {
      const post = captured.find(
        (c) =>
          c.method === "POST" && c.url.includes("/v1/personas/p1/adopted-apps"),
      );
      expect(post).toBeTruthy();
      const parsed = JSON.parse(post?.body ?? "{}");
      expect(parsed.catalog_name).toBe("secretless-image");
      expect(parsed.credential).toBeNull();
    });

    await waitFor(() =>
      expect(
        container.querySelector('[data-slot="app-setup-done"]'),
      ).toBeTruthy(),
    );
  });
});
