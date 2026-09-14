import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActivityState } from "@/components/activity-state";
import type { ActivityView } from "@/lib/activity";

const running = (id: string, label: string, kind = "web"): ActivityView => ({
  activityId: id,
  kind,
  name: "web_search",
  label,
  status: "running",
});

describe("ActivityState (P2 T5 — live using-X state)", () => {
  it("renders the current in-flight activity label with an ellipsis", () => {
    render(<ActivityState activities={[running("a1", "Searching the web")]} />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Searching the web…");
  });

  it("collapses to the CURRENT (latest) activity — not a stack", () => {
    render(
      <ActivityState
        activities={[
          { ...running("a1", "Searching the web"), status: "ok" },
          running("a2", "Running code", "sandbox"),
        ]}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Running code…");
    expect(screen.queryByText(/Searching the web/)).toBeNull();
  });

  it("clears (renders nothing) once the current activity resolves ok", () => {
    const { container } = render(
      <ActivityState
        activities={[{ ...running("a1", "Searching the web"), status: "ok" }]}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when there are no activities", () => {
    const { container } = render(<ActivityState activities={[]} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<ActivityState activities={undefined} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it("renders an awaiting_approval state without an ellipsis (A3 gate), dot static", () => {
    const { container } = render(
      <ActivityState
        activities={[
          { ...running("a1", "Spending $1500"), status: "awaiting_approval" },
        ]}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Spending $1500");
    expect(screen.getByRole("status").textContent).not.toContain("…");
    const dot = container.querySelector(".v-activity-dot");
    expect(dot?.getAttribute("data-awaiting")).toBe("true");
  });

  it("carries the kind onto the dot for theming + marks the dot decorative", () => {
    const { container } = render(
      <ActivityState
        activities={[running("a1", "Creating an image", "imagegen")]}
      />,
    );
    const dot = container.querySelector(".v-activity-dot");
    expect(dot?.getAttribute("data-kind")).toBe("imagegen");
    expect(dot?.getAttribute("aria-hidden")).toBe("true");
  });
});
