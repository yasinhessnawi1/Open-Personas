/**
 * R4 T5 — the single friendly-label source for a persona's "Apps".
 *
 * "Apps" is the unified user-facing term for what a persona can DO. Three
 * sources feed it, each normalised to a human label + a one-line "what it does"
 * so the UI NEVER shows a raw id (`datetime`, `mcp:google-flights`,
 * `document_generation`):
 *   - built-in TOOLS   — ids from `GET /v1/tools`  (canonical set: TOOL_CATALOG)
 *   - built-in SKILLS  — ids from `GET /v1/skills` (canonical set: _BUILTIN_SKILLS)
 *   - MCP servers      — `mcp:<name>`, labelled by the catalog `display_name`
 *                        (falling back to a humanised name for built-in servers
 *                        whose catalog entry declares none).
 *
 * Both the profile (info) surface and the persona editor import this module, so
 * a label is authored once. Labels + descriptions live in i18n
 * (`apps.catalog.<id>.{label,description}`) — the `check:no-literals` gate + the
 * single-source discipline. The contract test (`app-labels.test.ts`) is RED on
 * any built-in id here that lacks an en.json entry (mirrors the R4-C1-1
 * gallery-label lesson).
 */

export const MCP_PREFIX = "mcp:";

/**
 * Canonical built-in TOOL ids. MIRRORS the API source of truth
 * `packages/core/src/persona/tools/catalog.py` (`TOOL_CATALOG`). Keep in sync:
 * a new tool there needs a label here + an en.json entry, or the contract test
 * goes red.
 */
export const BUILTIN_TOOL_IDS = [
  "web_search",
  "web_fetch",
  "file_read",
  "file_write",
  "code_execution",
  "calculator",
  "datetime",
  "regex_match",
  "text_diff",
  "text_summarize",
  "mcp_search",
  "json_query",
  "currency_convert",
  "generate_image",
  "render_diagram",
] as const;

/**
 * Canonical built-in SKILL ids. MIRRORS
 * `packages/api/src/persona_api/services/catalog_service.py` (`_BUILTIN_SKILLS`).
 */
export const BUILTIN_SKILL_IDS = [
  "code_review",
  "data_analysis",
  "document_generation",
  "web_research",
] as const;

/** Every built-in app id the label map must cover (tools + skills). */
export const BUILTIN_APP_IDS: readonly string[] = [
  ...BUILTIN_TOOL_IDS,
  ...BUILTIN_SKILL_IDS,
];

export function isMcpId(id: string): boolean {
  return id.startsWith(MCP_PREFIX);
}

export function mcpName(id: string): string {
  return id.slice(MCP_PREFIX.length);
}

/**
 * Humanise an id / MCP name so the UI never shows a raw snake/kebab id — the
 * fallback for an unknown tool or a built-in MCP server with no `display_name`
 * (`time` → "Time", `google-flights` → "Google Flights").
 */
export function humanizeAppId(raw: string): string {
  const base = isMcpId(raw) ? mcpName(raw) : raw;
  return (
    base
      .split(/[\s_-]+/)
      .filter(Boolean)
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ") || base
  );
}

/** i18n key for a built-in app's friendly label (relative to the `apps` namespace). */
export function builtinLabelKey(id: string): string {
  return `catalog.${id}.label`;
}

/** i18n key for a built-in app's one-line description (relative to `apps`). */
export function builtinDescriptionKey(id: string): string {
  return `catalog.${id}.description`;
}

/** The minimal translate surface both `useTranslations` and `getTranslations` satisfy. */
export interface AppTranslate {
  (key: string, values?: Record<string, string | number>): string;
  has?: (key: string) => boolean;
}

/** MCP catalog lookup: name → its display metadata (from `GET /v1/mcp-catalog`). */
export type McpLookup = (
  name: string,
) => { displayName?: string | null; description?: string | null } | undefined;

export interface AppPresentation {
  readonly id: string;
  readonly label: string;
  readonly description: string | null;
}

/**
 * Resolve one app id to its friendly `{ label, description }`, never a raw id.
 *
 * @param id  a built-in tool/skill id, or an `mcp:<name>` id.
 * @param t   a translator scoped to the `apps` namespace.
 * @param mcp optional MCP catalog lookup for `mcp:` ids.
 */
export function presentApp(
  id: string,
  t: AppTranslate,
  mcp?: McpLookup,
): AppPresentation {
  if (isMcpId(id)) {
    const name = mcpName(id);
    const info = mcp?.(name);
    return {
      id,
      label: info?.displayName?.trim() || humanizeAppId(name),
      description: info?.description?.trim() || null,
    };
  }
  const labelKey = builtinLabelKey(id);
  const known = t.has ? t.has(labelKey) : true;
  if (known) {
    return {
      id,
      label: t(labelKey),
      description: t(builtinDescriptionKey(id)),
    };
  }
  // Unknown id (e.g. a bring-your-own tool) — humanise so no raw id leaks.
  return { id, label: humanizeAppId(id), description: null };
}
