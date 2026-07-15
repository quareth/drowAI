/**
 * TaskPanel task-action admission error presentation tests.
 *
 * Responsibilities:
 * - Verify structured start rejections produce Runner-focused user messages.
 * - Guard against collapsing offline Runner states into capacity wording.
 */
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TaskPanel } from "@/components/panels/task-panel";
import type { Task } from "@/types";

const mocked = vi.hoisted(() => ({
  actionReasonCode: "NO_RUNNERS_REGISTERED",
  tasks: [] as Task[],
  toast: vi.fn(),
}));

vi.mock("@/hooks/useTaskManagement", () => ({
  useTaskManagement: () => ({
    isLoading: false,
    tasks: mocked.tasks,
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
  useToast: () => ({ toast: mocked.toast }),
}));

vi.mock("@/hooks/use-reporting", () => ({
  canPrepareReportingInput: () => false,
  shouldRegeneratePreparedMemo: () => false,
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

vi.mock("@/hooks/use-task-panel", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/use-task-panel")>(
    "@/hooks/use-task-panel",
  );
  const { ApiResponseError } = await vi.importActual<typeof import("@/lib/response-error")>(
    "@/lib/response-error",
  );
  return {
    ...actual,
    useTaskPanelMutations: (options: {
      onTaskActionError?: (error: Error) => void;
    }) => ({
      taskActionMutation: {
        isPending: false,
        mutate: () => {
          options.onTaskActionError?.(
            new ApiResponseError("Task admission rejected.", {
              status: 409,
              detail: {
                reason_code: mocked.actionReasonCode,
                reason_codes: [mocked.actionReasonCode],
                message: "Task admission rejected.",
              },
              reasonCode: mocked.actionReasonCode,
              reasonCodes: [mocked.actionReasonCode],
            }),
          );
        },
      },
      deleteTaskMutation: {
        isPending: false,
        mutateAsync: vi.fn(),
      },
    }),
  };
});

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

function pendingTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 77,
    user_id: 1,
    name: "Pending task",
    scope: "example.com",
    status: "pending",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderPanel() {
  render(<TaskPanel />);
  fireEvent.click(screen.getByTitle("Flat view"));
}

describe("TaskPanel task admission error presentation", () => {
  beforeEach(() => {
    mocked.actionReasonCode = "NO_RUNNERS_REGISTERED";
    mocked.tasks = [pendingTask()];
    mocked.toast.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows Runner Site readiness guidance for start rejections without registered Runners", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => {
      expect(mocked.toast).toHaveBeenCalledWith({
        title: "Runner Site needs a Runner",
        description:
          "No Runner is registered yet. Open Runner Site settings and connect a Runner before creating or starting tasks.",
        variant: "destructive",
      });
    });
  });

  it("shows offline Runner guidance instead of capacity wording for stale Runner start rejections", async () => {
    mocked.actionReasonCode = "RUNNER_STALE_OR_OFFLINE";

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => {
      expect(mocked.toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Runner is not connected",
          variant: "destructive",
        }),
      );
    });
    expect(mocked.toast).not.toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Runner capacity is exhausted",
      }),
    );
  });
});
