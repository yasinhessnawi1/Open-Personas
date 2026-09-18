import { describe, expect, it } from "vitest";

import {
  clerkFrontendApiHost,
  clerkHostFromPublishableKey,
} from "./clerk-frontend-api-host";

const key = (env: "live" | "test", host: string): string =>
  `pk_${env}_${Buffer.from(`${host}$`, "utf8").toString("base64")}`;

describe("clerkHostFromPublishableKey", () => {
  it("decodes the Frontend API host a live key carries", () => {
    expect(clerkHostFromPublishableKey(key("live", "clerk.example.com"))).toBe(
      "clerk.example.com",
    );
  });

  it("decodes a development key the same way", () => {
    expect(
      clerkHostFromPublishableKey(
        key("test", "bright-fox-12.clerk.accounts.dev"),
      ),
    ).toBe("bright-fox-12.clerk.accounts.dev");
  });

  it.each([
    ["missing", undefined],
    ["empty", ""],
    ["not a publishable key", "sk_live_abc"],
    ["wrong instance prefix", "pk_prod_Y2xlcmsuZXhhbXBsZS5jb20k"],
    ["not base64 of a host", "pk_live_!!!!"],
    [
      "decoded value without the terminator",
      `pk_live_${Buffer.from("clerk.example.com").toString("base64")}`,
    ],
    [
      "decoded value that is not a hostname",
      `pk_live_${Buffer.from("not a host$").toString("base64")}`,
    ],
  ])("returns null for a %s key", (_label, value) => {
    expect(clerkHostFromPublishableKey(value)).toBeNull();
  });
});

describe("clerkFrontendApiHost", () => {
  it("prefers the explicit override over the key", () => {
    expect(
      clerkFrontendApiHost({
        NEXT_PUBLIC_CLERK_FRONTEND_API_HOST: "  clerk.override.example  ",
        NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: key("live", "clerk.example.com"),
      }),
    ).toBe("clerk.override.example");
  });

  it("falls back to the key when the override is unset or blank", () => {
    expect(
      clerkFrontendApiHost({
        NEXT_PUBLIC_CLERK_FRONTEND_API_HOST: "   ",
        NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: key("live", "clerk.example.com"),
      }),
    ).toBe("clerk.example.com");
  });

  it("never invents a host: nothing set means no rewrite target", () => {
    expect(clerkFrontendApiHost({})).toBeNull();
  });
});
