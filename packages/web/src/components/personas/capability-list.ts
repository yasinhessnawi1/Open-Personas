/**
 * R4 T4.5 — one shared scroll-list treatment for the persona editor's
 * capability menus (voice / apps / specialities / built-in tools).
 *
 * Each of those menus used to render its FULL catalogue as an unbounded list,
 * so a long set turned the whole editor into an endless page scroll. This caps
 * the list to roughly 6–8 rows and scrolls WITHIN it instead:
 *   - `max-h-96` — the height ceiling (~24rem) that triggers the inner scroll;
 *   - `overflow-y-auto` — the scrollbar lives inside the list, no layout jump;
 *   - `overscroll-contain` — the wheel/trackpad doesn't chain to the page at the
 *     list edges;
 *   - `pr-1` — a little gutter so a focus ring on the last row isn't clipped by
 *     the scrollbar (keyboard + scroll accessible, both themes).
 *
 * Applied by composing onto each list's existing flex layout via `cn`, so the
 * three menus share one treatment and stay visually consistent.
 */
export const CAPABILITY_SCROLL_LIST_CLASS =
  "max-h-96 overflow-y-auto overscroll-contain pr-1";
