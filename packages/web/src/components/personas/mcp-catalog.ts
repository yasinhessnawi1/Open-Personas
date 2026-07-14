import type { components } from "@/lib/api/schema";
import type {
  McpCatalogEntry,
  McpDeploymentCapabilities,
} from "./persona-form";

/**
 * Spec 30 T11 / N3 — map the API `MCPCatalogServer` rows (GET /v1/mcp-catalog)
 * onto the form's `McpCatalogEntry` shape (snake_case → camelCase). Shared by the
 * new + edit persona pages (and the N3 apps view) so the mapping has one home.
 *
 * N3 carries the Docker catalog-mirror display metadata + trust labels + the
 * display-only credential schema through to the component layer. Every additive
 * field is optional-with-default on the wire (additive-with-default backend
 * contract, D-N1-3), so a row from the old five-field contract maps to the same
 * empty defaults. The `secrets[]` schema is READ-only here (N3-D-1/10).
 */
export function mapMcpCatalog(
  rows: components["schemas"]["MCPCatalogServer"][],
): McpCatalogEntry[] {
  return rows.map((r) => ({
    name: r.name,
    description: r.description,
    provider: r.provider,
    defaultEnabled: r.default_enabled,
    requiredEnv: r.required_env ?? [],
    // -- N3: display metadata + trust labels + display-only credential schema --
    displayName: r.display_name ?? "",
    iconUrl: r.icon_url ?? "",
    image: r.image ?? "",
    serverType: r.server_type ?? "builtin",
    risk: r.risk ?? "low",
    sourceProject: r.source_project ?? "",
    sourceCommit: r.source_commit ?? "",
    signed: r.signed ?? false,
    allowHosts: r.allow_hosts ?? [],
    secrets: (r.secrets ?? []).map((s) => ({
      name: s.name,
      env: s.env,
      example: s.example ?? "",
      description: s.description ?? "",
    })),
    // -- Spec R8/N7: per-user OAuth binding passthrough --
    authMethod: r.auth_method ?? "",
    oauthProvider: r.oauth_provider ?? "",
  }));
}

/**
 * Spec N7 (D-N7-2) — map the API `MCPDeploymentCapabilities` wrapper field
 * (snake_case → camelCase). Every field is required on the wire (the route always
 * computes a truthful value), so no defaulting beyond the TS shape itself.
 */
export function mapMcpCapabilities(
  caps: components["schemas"]["MCPDeploymentCapabilities"],
): McpDeploymentCapabilities {
  return {
    perTenantRuntime: caps.per_tenant_runtime,
    gateway: caps.gateway,
    oauthProviders: caps.oauth_providers ?? [],
  };
}
