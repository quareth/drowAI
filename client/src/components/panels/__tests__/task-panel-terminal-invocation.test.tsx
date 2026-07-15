/**
 * Purpose: Ensure task actions trigger terminal open via typed workbench state.
 */
// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TaskPanel } from "@/components/panels/task-panel";

const openTerminalForTaskMock = vi.fn();

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
    tasks: [
      {
        id: 101,
        user_id: 1,
        name: "Runtime task",
        scope: "example scope",
        status: "running",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ],
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

vi.mock("@/state/chat-stream-store", () => ({
  clearTaskState: vi.fn(),
}));

vi.mock("@/state/chat-session-store", () => ({
  clearChatSession: vi.fn(),
}));

vi.mock("@/state/active-chat-task-store", () => ({
  getActiveChatTaskId: vi.fn(() => null),
  setActiveChatTaskId: vi.fn(),
}));

vi.mock("@/state/workbench-state-store", () => ({
  openTerminalForTask: (taskId: number) => openTerminalForTaskMock(taskId),
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
  DockerTerminal: () => null,
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

describe("TaskPanel terminal invocation", () => {
  beforeEach(() => {
    openTerminalForTaskMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("opens global terminal for the selected task from task actions", () => {
    render(<TaskPanel />);

    fireEvent.click(screen.getByTitle("Flat view"));
    fireEvent.click(screen.getByRole("button", { name: "Shell" }));

    expect(openTerminalForTaskMock).toHaveBeenCalledTimes(1);
    expect(openTerminalForTaskMock).toHaveBeenCalledWith(101);
  });
});
