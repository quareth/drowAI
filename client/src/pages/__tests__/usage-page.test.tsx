// @vitest-environment jsdom
/* Smoke tests for the dedicated `/usage` page.
 *
 * Focus:
 *  - The page renders the exact heading "Usage" (never "Dashboard").
 *  - With no task selected, the panel's empty state is visible and no
 *    insights hooks fire.
 *  - Selecting a task from the task selector forwards the chosen `taskId`
 *    into the panel (which is what drives the insights hook calls).
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import UsagePage from "@/pages/usage-page";
import type { Task } from "@/types";

// Avoid dragging the real navbar (which needs auth) and sidebar (which
// exercises wouter) into these smoke tests.
vi.mock("@/components/layout/navbar", () => ({
  Navbar: () => <div data-testid="navbar">navbar</div>,
}));

vi.mock("@/components/layout/sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar">sidebar</div>,
}));

// Capture every taskId the panel is rendered with. This is how we prove
// selector -> panel prop propagation without mounting the full insights UI.
const panelCalls: Array<number | null | undefined> = [];

vi.mock("@/components/task/usage-insights/usage-insights-panel", () => ({
  UsageInsightsPanel: ({ taskId }: { taskId: number | null | undefined }) => {
    panelCalls.push(taskId);
    if (taskId == null) {
      return (
        <div data-testid="panel-empty">
          Select a task to see usage insights.
        </div>
      );
    }
    return <div data-testid="panel-active">panel:{taskId}</div>;
  },
}));

// Spy on every insights hook: they must NOT be called while no task is
// selected. (The panel is mocked, so any call here would indicate that
// the page itself started fetching — which it must never do.)
const insightsSpy = {
  overview: vi.fn(),
  groups: vi.fn(),
  timeline: vi.fn(),
  records: vi.fn(),
};

vi.mock("@/hooks/useUsageInsights", () => ({
  useUsageInsightsOverview: (...args: unknown[]) => {
    insightsSpy.overview(...args);
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
  useUsageInsightsGroups: (...args: unknown[]) => {
    insightsSpy.groups(...args);
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
  useUsageInsightsTimeline: (...args: unknown[]) => {
    insightsSpy.timeline(...args);
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
  useUsageInsightsRecords: (...args: unknown[]) => {
    insightsSpy.records(...args);
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 42,
    user_id: 1,
    name: "Recon Scan",
    status: "running",
    created_at: "2026-04-14T10:00:00.000Z",
    updated_at: "2026-04-14T10:00:00.000Z",
    ...overrides,
  };
}

function renderPage(tasks: Task[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(["/api/tasks/"], tasks);
  return render(
    <QueryClientProvider client={client}>
      <UsagePage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  panelCalls.length = 0;
  insightsSpy.overview.mockReset();
  insightsSpy.groups.mockReset();
  insightsSpy.timeline.mockReset();
  insightsSpy.records.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("<UsagePage />", () => {
  it("renders the 'Usage' heading exactly (never 'Dashboard')", () => {
    renderPage([]);
    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading.textContent).toBe("Usage");
    expect(screen.queryByText("Dashboard")).toBeNull();
  });

  it("renders the panel empty state with null taskId and does not fire insights hooks", () => {
    renderPage([makeTask()]);

    // The mocked panel records exactly the prop it received.
    expect(panelCalls.length).toBeGreaterThan(0);
    expect(panelCalls[panelCalls.length - 1]).toBeNull();
    expect(screen.getByTestId("panel-empty")).toBeTruthy();

    // No insights endpoint calls before the user picks a task.
    expect(insightsSpy.overview).not.toHaveBeenCalled();
    expect(insightsSpy.groups).not.toHaveBeenCalled();
    expect(insightsSpy.timeline).not.toHaveBeenCalled();
    expect(insightsSpy.records).not.toHaveBeenCalled();
  });

  it("forwards the chosen taskId into the panel when the user selects a task", () => {
    renderPage([makeTask({ id: 7, name: "Web App Test" })]);

    // The Radix Select is a custom widget — to avoid depending on its
    // popover/portal internals we drive state by dispatching a change
    // on the native hidden combobox that Radix renders. The page's
    // onValueChange handler is agnostic to the trigger path.
    //
    // Fallback: wouter/radix inner semantics change across versions,
    // so we instead click the trigger and then the item by text.
    const trigger = screen.getByRole("combobox", { name: /select task/i });
    fireEvent.click(trigger);

    const option = screen.getByRole("option", { name: /Web App Test \(#7\)/ });
    fireEvent.click(option);

    // After selection the last prop the panel saw must be the new id.
    expect(panelCalls[panelCalls.length - 1]).toBe(7);
    expect(screen.getByTestId("panel-active").textContent).toBe("panel:7");
  });
});
