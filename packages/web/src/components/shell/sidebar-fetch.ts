import "server-only";

import { serverApi } from "@/lib/api/server";
import {
  rankPersonasByRecency,
  resolveCalls,
  resolveConversations,
  type SidebarData,
} from "./sidebar-data";

/** How many recent personas the rail surfaces + how many message / call rows to load. */
const RAIL_PERSONAS = 4;
const MESSAGE_ROWS = 30;
/** The sidebar Calls section is a recent-calls preview; the /calls page has the full history. */
const CALL_ROWS = 8;

/**
 * Resolve the sidebar's PERSONAS rail + MESSAGES list from data the app already
 * exposes (`GET /v1/personas`, `GET /v1/conversations`). No new endpoint, no
 * recency schema — the same derivation the dashboard uses (`rankPersonasByRecency`).
 *
 * Fail-soft: the sidebar is chrome, never the page's reason for being. Any fetch
 * failure (a cold token, a transient API blip) degrades to empty sections rather
 * than throwing and taking down every authenticated route.
 */
export async function fetchSidebarData(): Promise<SidebarData> {
  try {
    const api = await serverApi();
    const [personasRes, conversationsRes, callsRes, memoryRes] =
      await Promise.all([
        api.GET("/v1/personas"),
        api.GET("/v1/conversations", {
          params: { query: { limit: MESSAGE_ROWS, offset: 0 } },
        }),
        api.GET("/v1/calls", {
          params: { query: { limit: CALL_ROWS, offset: 0 } },
        }),
        // Spec K5: a bounded, fail-soft availability probe — gates the Memory nav
        // row by whether the deployment has a usable knowledge-graph (the window's
        // `available` flag). Parallel with the rest; any failure degrades to hidden.
        api.GET("/v1/memory/graph"),
      ]);
    const personas = personasRes.data ?? [];
    const conversations = conversationsRes.data ?? [];
    const calls = callsRes.data ?? [];

    return {
      personas: rankPersonasByRecency(personas, conversations).slice(
        0,
        RAIL_PERSONAS,
      ),
      conversations: resolveConversations(conversations, personas),
      calls: resolveCalls(calls, personas),
      memoryAvailable: memoryRes.data?.available ?? false,
    };
  } catch {
    return {
      personas: [],
      conversations: [],
      calls: [],
      memoryAvailable: false,
    };
  }
}
