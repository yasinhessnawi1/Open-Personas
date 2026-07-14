/**
 * N3 (MCP-as-apps) Task 1 — the data spine.
 *
 * `mapMcpCatalog` must carry the full Docker catalog-mirror surface through to
 * the component layer: the original five fields PLUS the N1 display metadata,
 * trust labels, and the display-only credential schema (snake_case → camelCase).
 * Back-compat: a row from the old five-field contract (the additive backend
 * fields absent on the wire) maps to the documented empty/defaults.
 */

import { describe, expect, it } from "vitest";
import type { components } from "@/lib/api/schema";
import { mapMcpCapabilities, mapMcpCatalog } from "./mcp-catalog";

type Row = components["schemas"]["MCPCatalogServer"];
type CapsRow = components["schemas"]["MCPDeploymentCapabilities"];

describe("mapMcpCatalog", () => {
  it("maps the full N1 display/trust/secrets surface (snake_case → camelCase)", () => {
    const row: Row = {
      name: "github",
      description: "GitHub MCP server",
      provider: "mcp:optional",
      default_enabled: false,
      required_env: ["GITHUB_TOKEN"],
      display_name: "GitHub",
      icon_url: "https://example.test/github.png",
      image: "ghcr.io/example/github:1.2.3",
      server_type: "server",
      risk: "medium",
      source_project: "https://github.com/docker/mcp-registry",
      source_commit: "0123456789abcdef0123456789abcdef01234567",
      signed: true,
      allow_hosts: ["api.github.com"],
      secrets: [
        {
          name: "github.personal_access_token",
          env: "GITHUB_PERSONAL_ACCESS_TOKEN",
          example: "<YOUR_TOKEN>",
          description: "A GitHub PAT with repo scope.",
        },
      ],
      auth_method: "oauth",
      oauth_provider: "github",
    };

    expect(mapMcpCatalog([row])).toEqual([
      {
        name: "github",
        description: "GitHub MCP server",
        provider: "mcp:optional",
        defaultEnabled: false,
        requiredEnv: ["GITHUB_TOKEN"],
        displayName: "GitHub",
        iconUrl: "https://example.test/github.png",
        image: "ghcr.io/example/github:1.2.3",
        serverType: "server",
        risk: "medium",
        sourceProject: "https://github.com/docker/mcp-registry",
        sourceCommit: "0123456789abcdef0123456789abcdef01234567",
        signed: true,
        allowHosts: ["api.github.com"],
        secrets: [
          {
            name: "github.personal_access_token",
            env: "GITHUB_PERSONAL_ACCESS_TOKEN",
            example: "<YOUR_TOKEN>",
            description: "A GitHub PAT with repo scope.",
          },
        ],
        authMethod: "oauth",
        oauthProvider: "github",
      },
    ]);
  });

  it("defaults the additive N1 + N7 fields when a back-compat five-field row omits them", () => {
    // The original spec-30 contract: the additive backend fields are absent on
    // the wire (optional-with-default), so they map to the documented empties.
    const legacyRow = {
      name: "time",
      description: "Clock + timezone tools",
      provider: "mcp:builtin",
      default_enabled: true,
    } as Row;

    expect(mapMcpCatalog([legacyRow])).toEqual([
      {
        name: "time",
        description: "Clock + timezone tools",
        provider: "mcp:builtin",
        defaultEnabled: true,
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
      },
    ]);
  });

  it("normalizes each secret's optional example/description to empty strings", () => {
    const row = {
      name: "thing",
      description: "x",
      provider: "mcp:optional",
      default_enabled: false,
      secrets: [{ name: "thing.key", env: "THING_KEY" }],
    } as Row;

    const [mapped] = mapMcpCatalog([row]);
    expect(mapped.secrets).toEqual([
      { name: "thing.key", env: "THING_KEY", example: "", description: "" },
    ]);
  });

  it("returns an empty list for an empty catalog", () => {
    expect(mapMcpCatalog([])).toEqual([]);
  });
});

describe("mapMcpCapabilities", () => {
  it("maps the wrapper's capabilities field (snake_case → camelCase)", () => {
    const caps: CapsRow = {
      per_tenant_runtime: true,
      gateway: false,
      oauth_providers: ["github"],
    };
    expect(mapMcpCapabilities(caps)).toEqual({
      perTenantRuntime: true,
      gateway: false,
      oauthProviders: ["github"],
    });
  });

  it("defaults oauth_providers to empty when omitted", () => {
    const caps = { per_tenant_runtime: false, gateway: true } as CapsRow;
    expect(mapMcpCapabilities(caps)).toEqual({
      perTenantRuntime: false,
      gateway: true,
      oauthProviders: [],
    });
  });
});
