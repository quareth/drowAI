/**
 * Verifies TaskPanel role-aware UI states from server-provided tenant actions.
 */
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TaskPanel } from "@/components/panels/task-panel";
import type { Task } from "@/types";

let mockedTasks: Task[] = [];
let mockedActions: string[] = [];

vi.mock("@tanstack/react-query", () => ({
  useMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

vi.mock("@/hooks/useTaskManagement", () => ({
  useTaskManagement: () => ({
    isLoading: false,
    tasks: mockedTasks,
  }),
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: {
      actions: mockedActions,
    },
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/hooks/use-reporting", () => ({
  canPrepareReportingInput: (row: { input_state: string; is_preparable: boolean }) =>
    row.is_preparable && ["not_prepared", "failed", "stale"].includes(row.input_state),
  shouldRegeneratePreparedMemo: (row: { input_state: string }) =>
    row.input_state === "failed" || row.input_state === "stale",
  usePrepareTaskMemo: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
    variables: undefined,
  }),
  useTaskPanelReportingStatusProjection: () => ({
    engagementId: null,
    inputByTaskId: new Map(),
    hasInventory: false,
    isLoading: false,
    isError: false,
    error: null,
  }),
}));

vi.mock("@/contexts/PlanContext", () => ({
  usePlanContext: () => ({ clearState: vi.fn() }),
}));

vi.mock("@/state/workbench-state-store", () => ({
  openTerminalForTask: vi.fn(),
}));

vi.mock("@/hooks/use-engagement-knowledge", () => ({
  invalidateEngagementKnowledgeQueries: vi.fn(),
  useArchiveEngagement: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useEngagements: () => ({
    data: { items: [] },
    isLoading: false,
  }),
  useRestoreEngagement: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

vi.mock("@/components/modals/new-task-modal", () => ({
  NewTaskModal: () => null,
}));

vi.mock("@/components/modals/new-engagement-modal", () => ({
  NewEngagementModal: () => null,
}));

vi.mock("@/components/modals/scope-details-modal", () => ({
  ScopeDetailsModal: () => null,
}));

vi.mock("@/components/docker-terminal", () => ({
  DockerTerminal: ({ canTaskControl = true }: { canTaskControl?: boolean }) =>
    canTaskControl ? <button type="button">VPN Retry</button> : null,
}));

vi.mock("@/components/resources-panel", () => ({
  ResourcesPanel: () => null,
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: vi.fn(),
  queryClient: {
    invalidateQueries: vi.fn(),
    setQueryData: vi.fn(),
    removeQueries: vi.fn(),
  },
}));

describe("TaskPanel permission gating", () => {
  beforeEach(() => {
    mockedTasks = [];
    mockedActions = [];
  });

  afterEach(() => {
    cleanup();
  });

  it("disables create actions for viewers without write permissions", () => {
    render(<TaskPanel />);

    expect((screen.getByRole("button", { name: "New" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "New Engagement" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Quick Task" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("hides runtime-control buttons for viewers without task.control", () => {
    mockedTasks = [
      {
        id: 101,
        user_id: 1,
        name: "Runtime task",
        scope: "example scope",
        status: "running",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedActions = ["task.read"];

    render(<TaskPanel />);

    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Shell" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Flat view" }));
    fireEvent.click(screen.getByRole("button", { name: "Task actions for Runtime task" }));
    expect(screen.queryByText("Container Status")).toBeNull();
    expect(screen.queryByRole("button", { name: "VPN Retry" })).toBeNull();
  });
});
