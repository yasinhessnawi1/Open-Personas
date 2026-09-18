"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/auth";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * How often to re-check `GET /v1/personas/{id}` for the auto-generated avatar,
 * and the hard ceiling on how long to keep checking. The avatar is generated in
 * a server-side background task after create (voice-pick + a Cloudflare
 * image-gen bounded at ~25s), so a ~2.5s cadence catches it within a poll or two
 * of completion, and ~40s comfortably covers the worst-case generation budget.
 */
const POLL_INTERVAL_MS = 2500;
const POLL_MAX_MS = 40_000;

/**
 * Bounded poll for a persona's `avatar_url` after create (async-persona-create).
 *
 * `POST /v1/personas` now returns immediately with `avatar_url=null` (F1's
 * default initials-mark renders); the avatar is filled in by a server-side
 * background task seconds later. This hook lets the persona landing surface swap
 * the default for the real avatar the moment it lands, WITHOUT a server round
 * trip — it polls `GET /v1/personas/{id}` and returns the avatar_url once set.
 *
 * **Strictly bounded + leak-safe (guards against the prior infinite-fetch bug
 * class, commit 5ae0e10 voice-selector OOM):**
 *   - Polls at most `POLL_MAX_MS / POLL_INTERVAL_MS` times, then STOPS.
 *   - Stops immediately once `avatar_url` is non-null (the goal is reached).
 *   - Does NOT poll at all when `initialAvatarUrl` is already set (server gave
 *     us the avatar; nothing to wait for).
 *   - The effect depends ONLY on `[personaId, initialAvatarUrl]` — never on the
 *     `getToken` identity, which a non-memoising auth host/test would churn each
 *     render → effect re-run → setState → re-render → unbounded loop. `getToken`
 *     is read through a ref instead (the exact fix the voice-selector landed).
 *   - Cancels in-flight fetch + clears the interval on unmount / id change
 *     (AbortController + clearInterval in cleanup).
 */
export function usePersonaAvatarPoll(
  personaId: string,
  initialAvatarUrl: string | null,
): string | null {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;

  const [avatarUrl, setAvatarUrl] = useState<string | null>(initialAvatarUrl);

  useEffect(() => {
    // Already have an avatar (server-rendered) → nothing to poll for.
    if (initialAvatarUrl) {
      setAvatarUrl(initialAvatarUrl);
      return;
    }

    let cancelled = false;
    let elapsed = 0;
    const controller = new AbortController();
    let timer: ReturnType<typeof setInterval> | null = null;

    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    async function checkOnce() {
      try {
        const token = await getTokenRef.current(
          TEMPLATE ? { template: TEMPLATE } : undefined,
        );
        const res = await fetch(
          `${API}/v1/personas/${encodeURIComponent(personaId)}`,
          {
            signal: controller.signal,
            headers: token ? { Authorization: `Bearer ${token}` } : {},
          },
        );
        if (cancelled || !res.ok) return;
        const body = (await res.json()) as {
          avatar_url?: string | null;
          avatar_status?: "pending" | "failed" | null;
        };
        if (cancelled) return;
        if (body.avatar_url) {
          setAvatarUrl(body.avatar_url);
          stop(); // goal reached — stop polling.
        } else if (body.avatar_status === "failed") {
          // The durable job gave up (dead-lettered after its retries): nothing
          // will arrive, so stop now rather than at the cap. The default mark
          // stays; the avatar editor offers a retry or an upload.
          stop();
        }
      } catch {
        // Network hiccup / abort → ignore; the next tick (if any) retries, and
        // the bound still stops us. Never throws into the render tree.
      }
    }

    timer = setInterval(() => {
      elapsed += POLL_INTERVAL_MS;
      if (elapsed > POLL_MAX_MS) {
        stop(); // hard ceiling — give up; the default avatar stays.
        return;
      }
      void checkOnce();
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      controller.abort();
      stop();
    };
  }, [personaId, initialAvatarUrl]);

  return avatarUrl;
}
