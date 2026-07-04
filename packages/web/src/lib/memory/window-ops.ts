/**
 * Spec K5 — pure window-mutation ops for the client working set (K5-D-2).
 *
 * The Memory view never holds the whole graph: it starts from the seed window and
 * grows by focus-expansion (merge), shrinks on deletion (remove), and is bounded
 * by an eviction cap so sustained traversal can't grow the drawn set without
 * limit. Kept framework-free + pure so the loading/windowing + edit/delete flows
 * are unit-testable (criterion 13) independent of React.
 */

import type { MemoryLinkEdge, MemoryNodeSummary } from "@/lib/api";

export interface GraphWindow {
  readonly nodes: readonly MemoryNodeSummary[];
  readonly links: readonly MemoryLinkEdge[];
}

const linkKey = (l: MemoryLinkEdge): string =>
  `${l.src_node_id}->${l.dst_node_id}-${l.link_type}`;

/** Merge a freshly-loaded focus window into the current one, de-duping by id/key. */
export function mergeWindow(
  current: GraphWindow,
  incoming: GraphWindow,
): GraphWindow {
  const nodeIds = new Set(current.nodes.map((n) => n.id));
  const nodes = [
    ...current.nodes,
    ...incoming.nodes.filter((n) => !nodeIds.has(n.id)),
  ];
  const keys = new Set(current.links.map(linkKey));
  const links = [
    ...current.links,
    ...incoming.links.filter((l) => !keys.has(linkKey(l))),
  ];
  return { nodes, links };
}

/** Remove a node (deletion) and every link incident to it. */
export function removeFromWindow(
  current: GraphWindow,
  id: string,
): GraphWindow {
  return {
    nodes: current.nodes.filter((n) => n.id !== id),
    links: current.links.filter(
      (l) => l.src_node_id !== id && l.dst_node_id !== id,
    ),
  };
}

/** The ids to never evict for a given focus: the focus node + its neighbours. */
export function protectedIds(
  links: readonly MemoryLinkEdge[],
  focusId: string | null,
): Set<string> {
  const keep = new Set<string>();
  if (!focusId) return keep;
  keep.add(focusId);
  for (const l of links) {
    if (l.src_node_id === focusId) keep.add(l.dst_node_id);
    if (l.dst_node_id === focusId) keep.add(l.src_node_id);
  }
  return keep;
}

/**
 * Bound the working set to `cap` nodes (K5-D-2 eviction). Keeps the protected set
 * (focus + neighbours) plus the most-recently-added nodes up to the cap, evicting
 * the oldest; prunes links to the surviving nodes. A no-op under the cap.
 */
export function capWindow(
  current: GraphWindow,
  keep: Set<string>,
  cap: number,
): GraphWindow {
  if (current.nodes.length <= cap) return current;
  const protectedNodes = current.nodes.filter((n) => keep.has(n.id));
  const rest = current.nodes.filter((n) => !keep.has(n.id));
  const room = Math.max(0, cap - protectedNodes.length);
  // Most-recently-added survive: focus-expansion appends, so the tail is newest.
  const keptNodes = [...protectedNodes, ...rest.slice(rest.length - room)];
  const keptIds = new Set(keptNodes.map((n) => n.id));
  const links = current.links.filter(
    (l) => keptIds.has(l.src_node_id) && keptIds.has(l.dst_node_id),
  );
  return { nodes: keptNodes, links };
}
