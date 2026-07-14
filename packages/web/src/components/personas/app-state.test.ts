/**
 * N3 (MCP-as-apps) Task 3 — the pure app-state model.
 *
 * Covers each state in isolation, every precedence collision (N3-D-9:
 * unavailable > enabled > needs-setup > available), the honesty boundary
 * (needs-setup = DECLARES a requirement, not "your credential is missing";
 * unavailable = enabled-then-removed), and the edge cases.
 */

import { describe, expect, it } from "vitest";
import {
  type AppState,
  declaresCredential,
  deriveAppState,
  isAppEnabled,
} from "./app-state";
import type { McpCatalogEntry, McpCatalogSecret } from "./persona-form";

const secret: McpCatalogSecret = {
  name: "github.personal_access_token",
  env: "GITHUB_PERSONAL_ACCESS_TOKEN",
  example: "<YOUR_TOKEN>",
  description: "A GitHub PAT.",
};

/** A catalog entry with sane empty defaults; override per test. */
function entry(over: Partial<McpCatalogEntry> = {}): McpCatalogEntry {
  return {
    name: "thing",
    description: "",
    provider: "mcp:optional",
    defaultEnabled: false,
    requiredEnv: [],
    displayName: "",
    iconUrl: "",
    image: "",
    serverType: "builtin",
    risk: "low",
    sourceProject: "",
    sourceCommit: "",
    signed: false,
    allowHosts: [],
    secrets: [],
    authMethod: "",
    oauthProvider: "",
    ...over,
  };
}

describe("isAppEnabled", () => {
  it("is true when the persona carries the app's mcp:<name> entry", () => {
    expect(isAppEnabled("github", ["mcp:github"])).toBe(true);
  });

  it("is false when the app is not in the tools list", () => {
    expect(isAppEnabled("github", ["mcp:time", "web_search"])).toBe(false);
  });

  it("ignores tool-level entries (mcp:<server>:<tool> — more than one colon)", () => {
    // `mcp:docker:fetch` enables a TOOL on a live server, not the server itself
    // (mirrors the backend _is_mcp_server_enablement one-colon rule).
    expect(isAppEnabled("docker", ["mcp:docker:fetch"])).toBe(false);
  });

  it("is false for an empty tools list", () => {
    expect(isAppEnabled("github", [])).toBe(false);
  });
});

describe("declaresCredential", () => {
  it("is true when the app lists a secret", () => {
    expect(declaresCredential(entry({ secrets: [secret] }))).toBe(true);
  });

  it("is true when the app lists a required env var", () => {
    expect(declaresCredential(entry({ requiredEnv: ["GITHUB_TOKEN"] }))).toBe(
      true,
    );
  });

  it("is false when the app declares no credentials", () => {
    expect(declaresCredential(entry())).toBe(false);
  });
});

describe("deriveAppState — each state in isolation", () => {
  it("available = no declared creds, not enabled, not unavailable", () => {
    expect(deriveAppState(entry({ name: "time" }), [], [])).toBe<AppState>(
      "available",
    );
  });

  it("needs-setup = the app DECLARES a credential requirement and is not enabled", () => {
    // Honesty: this is "the app declares it needs a credential", NOT "your
    // credential is missing" — N3 has no read-back of operator-set creds.
    expect(
      deriveAppState(entry({ name: "github", secrets: [secret] }), [], []),
    ).toBe<AppState>("needs-setup");
    expect(
      deriveAppState(
        entry({ name: "github", requiredEnv: ["GITHUB_TOKEN"] }),
        [],
        [],
      ),
    ).toBe<AppState>("needs-setup");
  });

  it("enabled = the app's mcp:<name> entry is in the persona's tools", () => {
    expect(
      deriveAppState(entry({ name: "github" }), ["mcp:github"], []),
    ).toBe<AppState>("enabled");
  });

  it("unavailable = the app name is in unavailable_mcp_servers", () => {
    expect(
      deriveAppState(entry({ name: "github" }), [], ["github"]),
    ).toBe<AppState>("unavailable");
  });
});

describe("deriveAppState — precedence collisions (unavailable > enabled > needs-setup > available)", () => {
  it("enabled ∧ needs-setup → enabled (an on app declaring a cred is still 'on')", () => {
    expect(
      deriveAppState(
        entry({ name: "github", secrets: [secret] }),
        ["mcp:github"],
        [],
      ),
    ).toBe<AppState>("enabled");
  });

  it("unavailable ∧ enabled → unavailable (removed-but-enabled is flagged, not healthy)", () => {
    // This IS the canonical unavailable case by contract: unavailable_mcp_servers
    // only ever contains enabled-then-removed names.
    expect(
      deriveAppState(entry({ name: "github" }), ["mcp:github"], ["github"]),
    ).toBe<AppState>("unavailable");
  });

  it("unavailable ∧ needs-setup → unavailable", () => {
    expect(
      deriveAppState(
        entry({ name: "github", secrets: [secret] }),
        [],
        ["github"],
      ),
    ).toBe<AppState>("unavailable");
  });

  it("the full stack (unavailable ∧ enabled ∧ needs-setup) → unavailable", () => {
    expect(
      deriveAppState(
        entry({ name: "github", secrets: [secret], requiredEnv: ["X"] }),
        ["mcp:github"],
        ["github"],
      ),
    ).toBe<AppState>("unavailable");
  });
});

describe("deriveAppState — edges", () => {
  it("empty tools + empty unavailable + no secrets → available", () => {
    expect(deriveAppState(entry(), [], [])).toBe<AppState>("available");
  });

  it("omitted unavailable list defaults to empty (chooser surfaces without persona detail)", () => {
    expect(
      deriveAppState(entry({ name: "github" }), ["mcp:github"]),
    ).toBe<AppState>("enabled");
    expect(deriveAppState(entry({ name: "time" }), [])).toBe<AppState>(
      "available",
    );
  });

  it("a tool-level mcp:<server>:<tool> entry does not count as enabling the app", () => {
    expect(
      deriveAppState(entry({ name: "docker" }), ["mcp:docker:fetch"], []),
    ).toBe<AppState>("available");
  });
});
