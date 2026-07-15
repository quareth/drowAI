/**
 * TaskPanel grouped-view behaviors for engagement status actions and archived visibility.
 */
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TaskPanel } from "@/components/panels/task-panel";
import type { Task } from "@/types";

const reportingMocks = vi.hoisted(() => ({
  useTaskPanelReportingStatusProjection: vi.fn(() => ({
    engagementId: null,
    inputByTaskId: new Map(),
    hasInventory: false,
    isLoading: false,
    isError: false,
    error: null,
  })),
}));

let mockedTasks: Task[] = [];
let mockedEngagementItems: Array<{ id: number; name: string; status: string | null }> = [];
let toastSpy = vi.fn();
let archiveMutateAsyncSpy = vi.fn();
let restoreMutateAsyncSpy = vi.fn();

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
      actions: ["task.create", "task.control", "task.delete", "knowledge.write"],
    },
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: toastSpy }),
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
  useTaskPanelReportingStatusProjection: reportingMocks.useTaskPanelReportingStatusProjection,
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
    mutateAsync: archiveMutateAsyncSpy,
    isPending: false,
  }),
  useEngagements: () => ({
    data: { items: mockedEngagementItems },
    isLoading: false,
  }),
  useRestoreEngagement: () => ({
    mutateAsync: restoreMutateAsyncSpy,
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

vi.mock("@/lib/queryClient", () => ({
  apiRequest: vi.fn(),
  queryClient: {
    invalidateQueries: vi.fn(),
    setQueryData: vi.fn(),
    removeQueries: vi.fn(),
  },
}));

describe("TaskPanel engagement grouped behavior", () => {
  beforeEach(() => {
    if (typeof window !== "undefined" && window.localStorage && typeof window.localStorage.clear === "function") {
      window.localStorage.clear();
    }
    mockedTasks = [];
    mockedEngagementItems = [];
    toastSpy = vi.fn();
    archiveMutateAsyncSpy = vi.fn().mockResolvedValue({ id: 0 });
    restoreMutateAsyncSpy = vi.fn().mockResolvedValue({ id: 0 });
    reportingMocks.useTaskPanelReportingStatusProjection.mockClear();
    reportingMocks.useTaskPanelReportingStatusProjection.mockReturnValue({
      engagementId: null,
      inputByTaskId: new Map(),
      hasInventory: false,
      isLoading: false,
      isError: false,
      error: null,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows Restore action for archived engagement groups", async () => {
    mockedTasks = [
      {
        id: 101,
        user_id: 1,
        engagement_id: 10,
        engagement_name: "Archive Group",
        name: "Task A",
        status: "running",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedEngagementItems = [{ id: 10, name: "Archive Group", status: "archived" }];

    render(<TaskPanel />);

    const actionsButton = screen.getByLabelText("Engagement actions for Archive Group");
    fireEvent.pointerDown(actionsButton);
    fireEvent.click(actionsButton);

    expect(await screen.findByText("Restore Engagement")).toBeTruthy();
    expect(screen.queryByText("Archive Engagement")).toBeNull();
  });

  it("selects one expanded engagement for reporting inventory projection", () => {
    mockedTasks = [
      {
        id: 104,
        user_id: 1,
        engagement_id: 14,
        engagement_name: "Projection Group",
        name: "Task D",
        status: "stopped",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      {
        id: 105,
        user_id: 1,
        engagement_id: 14,
        engagement_name: "Projection Group",
        name: "Task E",
        status: "stopped",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedEngagementItems = [{ id: 14, name: "Projection Group", status: "active" }];

    render(<TaskPanel />);

    expect(reportingMocks.useTaskPanelReportingStatusProjection).toHaveBeenLastCalledWith(null);

    fireEvent.click(screen.getByRole("button", { name: "Projection Group" }));

    expect(reportingMocks.useTaskPanelReportingStatusProjection).toHaveBeenLastCalledWith(14);
  });

  it("hides archive/restore actions when engagement status is unknown", () => {
    mockedTasks = [
      {
        id: 102,
        user_id: 1,
        engagement_id: 11,
        engagement_name: "Unknown Group",
        name: "Task B",
        status: "running",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedEngagementItems = [];

    render(<TaskPanel />);

    const actionsButton = screen.getByLabelText("Engagement actions for Unknown Group");
    fireEvent.pointerDown(actionsButton);
    fireEvent.click(actionsButton);

    expect(screen.getByText("View Knowledge")).toBeTruthy();
    expect(screen.queryByText("Restore Engagement")).toBeNull();
    expect(screen.queryByText("Archive Engagement")).toBeNull();
  });

  it("shows archived synthetic groups only when archived toggle is enabled", () => {
    mockedTasks = [];
    mockedEngagementItems = [
      { id: 20, name: "Active Project", status: "active" },
      { id: 21, name: "Archived Project", status: "archived" },
    ];

    render(<TaskPanel />);

    expect(screen.getByText("Active Project")).toBeTruthy();
    expect(screen.queryByText("Archived Project")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show archived" }));
    expect(screen.getByText("Archived Project")).toBeTruthy();
  });

  it("filters visible groups by engagement name from the toolbar filter", () => {
    mockedTasks = [
      {
        id: 401,
        user_id: 1,
        engagement_id: 40,
        engagement_name: "Client Alpha",
        name: "Credential Review",
        status: "stopped",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      {
        id: 402,
        user_id: 1,
        engagement_id: 41,
        engagement_name: "Client Beta",
        name: "Credential Review",
        status: "stopped",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedEngagementItems = [
      { id: 40, name: "Client Alpha", status: "active" },
      { id: 41, name: "Client Beta", status: "active" },
    ];

    render(<TaskPanel />);

    fireEvent.click(screen.getByRole("button", { name: "Filter tasks and engagements" }));
    fireEvent.change(screen.getByLabelText("Task or engagement name filter"), {
      target: { value: "Alpha" },
    });

    expect(screen.getByText("Client Alpha")).toBeTruthy();
    expect(screen.queryByText("Client Beta")).toBeNull();
  });

  it("blocks archive only when runtime-active tasks are present", async () => {
    mockedTasks = [
      {
        id: 201,
        user_id: 1,
        engagement_id: 30,
        engagement_name: "Runtime Active Group",
        name: "Task Active",
        status: "running",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      {
        id: 202,
        user_id: 1,
        engagement_id: 30,
        engagement_name: "Runtime Active Group",
        name: "Task Stopped",
        status: "stopped",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedEngagementItems = [{ id: 30, name: "Runtime Active Group", status: "active" }];

    render(<TaskPanel />);

    const actionsButton = screen.getByLabelText("Engagement actions for Runtime Active Group");
    fireEvent.pointerDown(actionsButton);
    fireEvent.click(actionsButton);
    fireEvent.click(await screen.findByText("Archive Engagement"));

    expect(archiveMutateAsyncSpy).not.toHaveBeenCalled();
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Archive blocked",
        description:
          "Stop/retire runtime-active tasks before archiving. You do not need to delete stopped, failed, or completed tasks.",
      }),
    );
  });

  it("blocks archive even when filtered grouped tasks hide active runtime tasks", async () => {
    mockedTasks = [
      {
        id: 211,
        user_id: 1,
        engagement_id: 32,
        engagement_name: "Filtered Guard Group",
        name: "Task Running",
        status: "running",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      {
        id: 212,
        user_id: 1,
        engagement_id: 32,
        engagement_name: "Filtered Guard Group",
        name: "Task Stopped",
        status: "stopped",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedEngagementItems = [{ id: 32, name: "Filtered Guard Group", status: "active" }];

    render(<TaskPanel statusFilter="stopped" />);

    const actionsButton = screen.getByLabelText("Engagement actions for Filtered Guard Group");
    fireEvent.pointerDown(actionsButton);
    fireEvent.click(actionsButton);
    fireEvent.click(await screen.findByText("Archive Engagement"));

    expect(archiveMutateAsyncSpy).not.toHaveBeenCalled();
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Archive blocked",
        description:
          "Stop/retire runtime-active tasks before archiving. You do not need to delete stopped, failed, or completed tasks.",
      }),
    );
  });

  it("allows archive when engagement tasks are runtime-retired or terminal", async () => {
    mockedTasks = [
      {
        id: 301,
        user_id: 1,
        engagement_id: 31,
        engagement_name: "Terminal Group",
        name: "Task Stopped",
        status: "stopped",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      {
        id: 302,
        user_id: 1,
        engagement_id: 31,
        engagement_name: "Terminal Group",
        name: "Task Completed",
        status: "completed",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      {
        id: 303,
        user_id: 1,
        engagement_id: 31,
        engagement_name: "Terminal Group",
        name: "Task Failed",
        status: "failed",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    mockedEngagementItems = [{ id: 31, name: "Terminal Group", status: "active" }];

    render(<TaskPanel />);

    const actionsButton = screen.getByLabelText("Engagement actions for Terminal Group");
    fireEvent.pointerDown(actionsButton);
    fireEvent.click(actionsButton);
    fireEvent.click(await screen.findByText("Archive Engagement"));

    expect(window.confirm).toHaveBeenCalledWith(
      'Archive engagement "Terminal Group"? Knowledge and findings will be preserved.',
    );
    expect(archiveMutateAsyncSpy).toHaveBeenCalledWith(31);
    expect(toastSpy).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Archive blocked" }),
    );
  });
});
