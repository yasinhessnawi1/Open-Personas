/**
 * Spec A6 (W7) — <AutonomyControls> tests: the kill switches + the HONESTY CONSTRAINT.
 *
 * - **Honest copy** — the owner pause says what it truly does now (stops running tasks at their
 *   next step), and NEVER claims to "stop all autonomy" (origination isn't gated until merge-back).
 * - **Durable reflection** — the toggle reflects the durable post-state (idempotent, calm).
 * - **Calm landing** — the per-persona rows are behind a disclosure (no fetch until managed).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";

import { AutonomyControls } from "./autonomy-controls";

const autonomy = vi.hoisted(() => ({
  getAutonomyState: vi.fn(),
  pauseAutonomy: vi.fn(),
  resumeAutonomy: vi.fn(),
  getPersonaSuspension: vi.fn(),
  suspendPersona: vi.fn(),
  resumePersona: vi.fn(),
}));
const tasks = vi.hoisted(() => ({
  getInitiativeDial: vi.fn(),
  setInitiativeDial: vi.fn(),
}));
const auth = vi.hoisted(() => ({
  getToken: () => Promise.resolve("test-token"),
}));

vi.mock("@/auth", () => ({ useAuth: () => ({ getToken: auth.getToken }) }));
vi.mock("@/components/patterns/toast", () => ({
  useToast: () => ({ error: vi.fn(), success: vi.fn() }),
}));
vi.mock("@/lib/api/autonomy-client", () => autonomy);
vi.mock("@/lib/api/tasks-client", () => tasks);

function renderControls(personas = [{ id: "kai", name: "Kai" }]) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <AutonomyControls personas={personas} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  autonomy.getAutonomyState.mockResolvedValue({
    paused: false,
    changed: false,
    note: "",
  });
  autonomy.getPersonaSuspension.mockResolvedValue({
    persona_id: "kai",
    suspended: false,
    changed: false,
    note: "",
  });
  tasks.getInitiativeDial.mockResolvedValue({
    persona_id: "kai",
    dial: "propose_only",
    changed: false,
    initiative_enabled: false,
    note: "",
  });
});

describe("AutonomyControls", () => {
  it("states the true, narrow scope and never claims to stop ALL autonomy", async () => {
    renderControls();
    expect(await screen.findByText("Active")).toBeInTheDocument();
    // the honest hint — what a pause actually does now (task-leg scoped).
    expect(
      screen.getByText(/running tasks at their next step/i),
    ).toBeInTheDocument();
    // the forbidden overclaim must NOT appear anywhere.
    expect(screen.queryByText(/stops all autonomy/i)).toBeNull();
    expect(screen.queryByText(/all autonomy/i)).toBeNull();
  });

  it("reflects the durable post-state on toggle (idempotent, calm)", async () => {
    autonomy.pauseAutonomy.mockResolvedValue({
      paused: true,
      changed: true,
      note: "",
    });
    renderControls();
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    // after the durable reflection, the state reads Paused + offers Resume.
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(autonomy.pauseAutonomy).toHaveBeenCalledOnce();
  });

  it("keeps the per-persona rows behind a disclosure (calm landing)", async () => {
    renderControls();
    await screen.findByText("Active");
    // not fetched/rendered until the user opens the section.
    expect(autonomy.getPersonaSuspension).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: /manage each persona/i }),
    );
    expect(await screen.findByText("Kai")).toBeInTheDocument();
    expect(autonomy.getPersonaSuspension).toHaveBeenCalledWith(
      "test-token",
      "kai",
    );
  });
});
