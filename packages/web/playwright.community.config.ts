import { defineConfig, devices } from "@playwright/test";

/**
 * The COMMUNITY oracle config (Spec W1).
 *
 * The default config signs a Clerk user in; the community edition has no Clerk, so this one
 * drives a plain browser against a community API + web pair the operator has already started.
 * It is deliberately a separate `testDir` so the cloud e2e run (testDir `./e2e`) never picks
 * these up, and it starts no server of its own: the harness is the point of the exercise, and
 * the runbook is in the spec's evidence file.
 *
 *   PERSONA_EDITION=community PERSONA_COMMUNITY_DB_MODE=embedded \
 *   PERSONA_API_CORS_ORIGINS="http://localhost:3000,http://localhost:3077" \
 *   ... uv run uvicorn persona_api.dev:create_app --factory --port 8077
 *   PERSONA_EDITION=community NEXT_PUBLIC_API_BASE_URL=http://localhost:8077 pnpm dev -p 3077
 *   pnpm exec playwright test -c playwright.community.config.ts
 */
export default defineConfig({
  testDir: "./e2e-community",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  timeout: 180_000,
  use: {
    baseURL: process.env.E2E_COMMUNITY_BASE_URL ?? "http://localhost:3077",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
