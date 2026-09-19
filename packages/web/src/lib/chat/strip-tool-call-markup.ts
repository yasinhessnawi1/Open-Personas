/**
 * Remove a model's tool-call markup from text that is about to be shown.
 *
 * Some models write the tool call they meant to make as TEXT in the content
 * stream instead of calling properly, and the user reads raw markup in the
 * middle of the persona's prose:
 *
 *   <tool_call>schedule_introspect(scope: "all", days_ahead: 7)The schedule...
 *   <tool_call>datetime tool_call: </arg_value><arg_key>tool": "mcp_search"...
 *
 * The runtime now catches this at the provider boundary, so new messages
 * arrive clean. This is the render-side guard for the ones already written to
 * the database: a reopened conversation must not show markup either. It also
 * hides a half-arrived opening tag mid-stream, so `<tool_c` never flashes.
 *
 * Deliberately blunt: markup this recognises is removed even when a model was
 * quoting it on purpose. Showing it is the defect; removing it is the fix.
 */

const OPEN = "<tool_call>";
const CLOSE = "</tool_call>";

/** Markup that only ever appears as part of a leaked tool call. */
const ORPHANS = [
  CLOSE,
  "<arg_key>",
  "</arg_key>",
  "<arg_value>",
  "</arg_value>",
];

/** An orphan opener and the closer whose content goes with it. */
const ORPHAN_PAIRS: Record<string, string> = {
  "<arg_key>": "</arg_key>",
  "<arg_value>": "</arg_value>",
};

const HEAD_PAREN = /^\s*[A-Za-z_][A-Za-z0-9_.:-]{0,63}\s*\(/;
const HEAD_BRACE = /^\s*\{/;

/** Index just past the bracket that balances the one at `start`, or -1. */
function scanBalanced(
  text: string,
  start: number,
  opener: string,
  closer: string,
): number {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (escaped) escaped = false;
    else if (ch === "\\") escaped = true;
    else if (inString) {
      if (ch === '"') inString = false;
    } else if (ch === '"') inString = true;
    else if (ch === opener) depth++;
    else if (ch === closer) {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

/**
 * How far the leaked region starting at `rest` runs.
 *
 * Whichever lands first: an explicit `</tool_call>`, the bracket that balances
 * a call-signature or JSON head, or a blank line. The blank line is the
 * backstop for the mangled shapes that close nothing at all: it bounds the
 * removal to one paragraph rather than the rest of the reply.
 */
function regionLength(rest: string): number {
  const candidates: number[] = [];

  const close = rest.indexOf(CLOSE);
  if (close !== -1) candidates.push(close + CLOSE.length);

  const paren = HEAD_PAREN.exec(rest);
  if (paren) {
    const end = scanBalanced(rest, paren[0].length - 1, "(", ")");
    if (end !== -1) candidates.push(end);
  }

  const brace = HEAD_BRACE.exec(rest);
  if (brace) {
    const end = scanBalanced(rest, brace[0].length - 1, "{", "}");
    if (end !== -1) candidates.push(end);
  }

  const blank = rest.indexOf("\n\n");
  if (blank !== -1) candidates.push(blank);

  return candidates.length > 0 ? Math.min(...candidates) : rest.length;
}

/** True when `tail` could still grow into one of the markers. */
function isPartialMarker(tail: string): boolean {
  if (tail.length < 2) return false;
  return [OPEN, ...ORPHANS].some((m) => m.startsWith(tail));
}

/**
 * Strip leaked tool-call markup from `text`.
 *
 * @param text - Message text as stored or as streamed so far.
 * @returns The text with every recognised fragment removed. Text that carries
 *   no markup is returned unchanged (same reference).
 */
export function stripToolCallMarkup(text: string): string {
  if (!text) return text;
  if (!text.includes("<")) return text;

  let out = "";
  let i = 0;
  let touched = false;

  while (i < text.length) {
    const next = text.indexOf("<", i);
    if (next === -1) {
      out += text.slice(i);
      break;
    }
    out += text.slice(i, next);
    const rest = text.slice(next);

    if (rest.startsWith(OPEN)) {
      touched = true;
      i = next + OPEN.length + regionLength(rest.slice(OPEN.length));
      continue;
    }
    const orphan = ORPHANS.find((m) => rest.startsWith(m));
    if (orphan) {
      touched = true;
      // An orphan OPENER takes its value with it. Otherwise the argument
      // itself ("Europe/Oslo") still reads as stray words in the prose.
      const closer = ORPHAN_PAIRS[orphan];
      const end = closer ? rest.indexOf(closer, orphan.length) : -1;
      i = next + (end === -1 ? orphan.length : end + closer.length);
      continue;
    }
    if (isPartialMarker(rest)) {
      // A tag still arriving mid-stream. Hide it rather than flash it.
      touched = true;
      break;
    }
    out += "<";
    i = next + 1;
  }

  return touched ? out : text;
}
