/**
 * Spec 30 T12 — BYO-MCP manager: list, add (with credential), assign.
 *
 * Mocks fetch (openapi-fetch hits global fetch) to serve the user's servers +
 * this persona's assignments, and asserts the add body carries the bearer
 * credential and the assign toggle PUTs the persona↔server link.
 */

import { render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { ByoMcpManager } from "./byo-mcp-manager";

vi.mock("@clerk/nextjs", () => ({
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

function installFetch(): { captured: Captured[]; restore: () => void } {
  const captured: Captured[] = [];
  const original = globalThis.fetch;
  const server = {
    id: "srv_1",
    name: "my-server",
    url: "https://example.com/mcp",
    auth_method: "none",
    enabled: true,
    has_credential: false,
    discovered_tools: null,
    created_at: "2026-06-16T00:00:00Z",
    updated_at: "2026-06-16T00:00:00Z",
  };
  globalThis.fetch = vi.fn(
    async (input: string | URL | Request, init?: RequestInit) => {
      const isReq = typeof input === "object" && "method" in input;
      const req = isReq ? (input as Request) : null;
      const url = req ? req.url : input.toString();
      const method = init?.method ?? req?.method ?? "GET";
      let body = typeof init?.body === "string" ? init.body : "";
      if (!body && req) body = await req.clone().text();
      captured.push({ url, method, body });

      if (method === "GET" && url.endsWith("/v1/mcp-servers")) {
        return jsonResponse([server]);
      }
      if (method === "GET" && url.includes("/personas/p1/mcp-servers")) {
        return jsonResponse([]); // not assigned yet
      }
      if (method === "POST" && url.endsWith("/v1/mcp-servers")) {
        return jsonResponse(server, 201);
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

function renderManager() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ByoMcpManager personaId="p1" />
    </NextIntlClientProvider>,
  );
}

describe("ByoMcpManager (spec 30 T12)", () => {
  let restore: () => void;
  afterEach(() => restore?.());

  it("lists the user's MCP servers after load", async () => {
    const { restore: r } = installFetch();
    restore = r;
    const { container } = renderManager();
    await waitFor(() => {
      expect(
        container.querySelectorAll('[data-slot="byo-server"]').length,
      ).toBe(1);
    });
  });

  it("adds a server with a bearer credential in the request body", async () => {
    const { captured, restore: r } = installFetch();
    restore = r;
    const { container, getByLabelText } = renderManager();
    await waitFor(() =>
      expect(
        container.querySelector('[data-slot="byo-server-list"]'),
      ).toBeTruthy(),
    );

    const fire = (el: Element, value: string) => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )?.set;
      setter?.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    };
    fire(getByLabelText("Name"), "prod");
    fire(getByLabelText("Server URL (https)"), "https://prod.example/mcp");
    const select = container.querySelector("select") as HTMLSelectElement;
    const nativeSet = Object.getOwnPropertyDescriptor(
      window.HTMLSelectElement.prototype,
      "value",
    )?.set;
    nativeSet?.call(select, "bearer");
    select.dispatchEvent(new Event("change", { bubbles: true }));
    fire(getByLabelText("Token"), "secret-123");

    const addBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Add server"),
    );
    addBtn?.click();

    await waitFor(() => {
      const post = captured.find(
        (c) => c.method === "POST" && c.url.endsWith("/v1/mcp-servers"),
      );
      expect(post).toBeTruthy();
      expect(JSON.parse(post?.body ?? "{}")).toEqual({
        name: "prod",
        url: "https://prod.example/mcp",
        auth_method: "bearer",
        credential: "secret-123",
      });
    });
  });

  it("assigns a server to the persona via PUT", async () => {
    const { captured, restore: r } = installFetch();
    restore = r;
    const { container } = renderManager();
    await waitFor(() =>
      expect(container.querySelector('[data-slot="byo-assign"]')).toBeTruthy(),
    );
    (
      container.querySelector('[data-slot="byo-assign"]') as HTMLButtonElement
    ).click();
    await waitFor(() => {
      expect(
        captured.some(
          (c) =>
            c.method === "PUT" &&
            c.url.includes("/v1/personas/p1/mcp-servers/srv_1"),
        ),
      ).toBe(true);
    });
  });

  it("reload() dispatches both GETs concurrently and stays fail-soft when one rejects", async () => {
    // Regression pin: reload() used to do
    // `Promise.all([unwrap(await api.GET(a)), unwrap(await api.GET(b))])`,
    // which awaits `a` to completion before `b` is even requested (no real
    // parallelism) and can race an early rejection as unhandled. Hold the
    // first response open to prove the second is requested anyway, then fail
    // the first and confirm it degrades the same way a fresh, empty mount
    // would — never as a Node-level unhandled rejection.
    const captured: Captured[] = [];
    const original = globalThis.fetch;
    let releaseServers: ((response: Response) => void) | undefined;
    const serversGate = new Promise<Response>((resolve) => {
      releaseServers = resolve;
    });
    globalThis.fetch = vi.fn(
      async (input: string | URL | Request, init?: RequestInit) => {
        const isReq = typeof input === "object" && "method" in input;
        const req = isReq ? (input as Request) : null;
        const url = req ? req.url : input.toString();
        const method = init?.method ?? req?.method ?? "GET";
        captured.push({ url, method, body: "" });
        if (method === "GET" && url.endsWith("/v1/mcp-servers")) {
          return serversGate; // held open until released below
        }
        if (method === "GET" && url.includes("/personas/p1/mcp-servers")) {
          return jsonResponse([]);
        }
        return new Response(null, { status: 204 });
      },
    ) as unknown as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };

    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);

    try {
      const { container } = renderManager();

      // The persona-scoped GET must show up even though /v1/mcp-servers is
      // still pending — this only holds if reload() fires both concurrently
      // instead of awaiting the first before starting the second.
      await waitFor(() => {
        expect(captured.some((c) => c.url.endsWith("/v1/mcp-servers"))).toBe(
          true,
        );
        expect(
          captured.some((c) => c.url.includes("/personas/p1/mcp-servers")),
        ).toBe(true);
      });

      // Now fail the still-pending request.
      releaseServers?.(jsonResponse({ error: "boom" }, 500));

      // Flush macrotask ticks so reload()'s rejection reaches the effect's
      // `.catch(() => {})` and Node gets a chance to flag anything left
      // unhandled.
      await new Promise((resolve) => setTimeout(resolve, 0));
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(unhandled).not.toHaveBeenCalled();
      // Fail-soft: same empty state as a fresh mount, not a crash.
      expect(
        container.querySelectorAll('[data-slot="byo-server"]').length,
      ).toBe(0);
    } finally {
      process.off("unhandledRejection", unhandled);
    }
  });
});
