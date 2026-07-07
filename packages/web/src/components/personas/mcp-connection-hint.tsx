import { isMcpId, mcpName } from "@/lib/apps/app-labels";
import { McpConnectionBadge } from "./mcp-connection-badge";
import type { McpConnectionStatus } from "./mcp-connection-label";

/**
 * N6 merge-back — the PROFILE surface's subtle not-connected signal.
 *
 * On "What <persona> can do" the happy path stays clean: a CONNECTED MCP app
 * shows nothing (no badge clutter), and built-in tools/skills never carry a
 * connection state at all. Only an MCP-sourced app whose runtime reports
 * not-connected gets the friendly {@link McpConnectionBadge} ("Starting…",
 * "Needs setup", "Not connected" — never the raw enum, R4-C1-21).
 *
 * Fail-soft by construction: an empty `connections` list (older api, community
 * without the runtime, fetch failure) simply renders nothing.
 */
export function McpConnectionHint({
  appId,
  connections,
}: {
  /** The app id as stored in the persona YAML (`mcp:<name>` or a bare tool id). */
  appId: string;
  connections: readonly McpConnectionStatus[];
}) {
  if (!isMcpId(appId)) return null;
  const name = mcpName(appId);
  const status = connections.find((c) => c.server_name === name);
  if (!status || status.connected) return null;
  return (
    <span data-slot="mcp-connection-hint">
      <McpConnectionBadge connected={status.connected} reason={status.reason} />
    </span>
  );
}
