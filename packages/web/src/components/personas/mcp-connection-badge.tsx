import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import {
  type ConnectionReason,
  type ConnectionTone,
  connectionBadge,
} from "./mcp-connection-label";

/**
 * N6 (Per-Tenant MCP) T7 — the not-connected badge.
 *
 * Renders one assigned MCP server's connection status as a FRIENDLY, plain-language
 * badge (R4 apps-grammar): the raw backend `reason` enum never appears — it is mapped
 * (via {@link connectionBadge}) to an `apps.connection.*` translation + a tone. A
 * connected server reads "Connected"; anything else names why in words ("Starting…",
 * "Needs setup", "Not connected"). This is the user-visible half of R4-C1-21.
 */
const TONE_VARIANT: Record<
  ConnectionTone,
  "secondary" | "outline" | "destructive"
> = {
  ok: "secondary",
  pending: "outline",
  warn: "destructive",
};

export function McpConnectionBadge({
  connected,
  reason,
}: {
  connected: boolean;
  reason: ConnectionReason | null;
}) {
  const t = useTranslations();
  const badge = connectionBadge({ connected, reason });
  return (
    <Badge
      variant={TONE_VARIANT[badge.tone]}
      data-slot="mcp-connection-badge"
      data-tone={badge.tone}
    >
      {t(badge.labelKey)}
    </Badge>
  );
}
