/**
 * TaskPanel reporting shortcut tests for inventory-backed prepare actions.
 */
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TaskPanel } from "@/components/panels/task-panel";
import type { Task } from "@/types";
import type { ReportingInputState, ReportingInputTaskRow } from "@/types/reporting";

const reportingMocks = vi.hoisted(() => ({
  useTaskPanelReportingStatusProjection: vi.fn(),
  prepareMutateAsync: vi.fn(),
  prepareIsPending: false,
  prepareVariables: undefined as { task_id: number } | undefined,
}));

let mockedTasks: Task[] = [];
let toastSpy = vi.fn();

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
    mutateAsync: reportingMocks.prepareMutateAsync,
    isPending: reportingMocks.prepareIsPending,
    variables: reportingMocks.prepareVariables,
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
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useEngagements: () => ({
    data: { items: [{ id: 7, name: "Client Alpha", status: "active" }] },
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

function task(id: number, name: string): Task {
  return {
    id,
    user_id: 1,
    engagement_id: 7,
    engagement_name: "Client Alpha",
    name,
    scope: "example scope",
    status: "stopped",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function row(
  taskId: number,
  inputState: ReportingInputState,
  overrides: Partial<ReportingInputTaskRow> = {},
): ReportingInputTaskRow {
  return {
    task_id: taskId,
    task_name: `Task ${taskId}`,
    task_status: "stopped",
    runtime_retired: true,
    is_reportable: true,
    is_preparable: inputState !== "ready" && inputState !== "preparing",
    memo_mode: "supported",
    not_preparable_reason: null,
    input_state: inputState,
    current_memo: null,
    latest_memo_attempt: null,
    source_watermark: {
      last_chat_message_id: null,
      last_turn_sequence: null,
      latest_tool_execution_id: null,
      latest_evidence_created_at: null,
      latest_knowledge_observed_at: null,
    },
    counts: {
      evidence: 1,
      canonical_findings: 1,
      candidate_findings: 0,
    },
    candidate_findings_require_explicit_inclusion: false,
    ...overrides,
  };
}

function renderExpandedPanel(rows: ReportingInputTaskRow[]) {
  reportingMocks.useTaskPanelReportingStatusProjection.mockImplementation((engagementId: number | null) => {
    const inputByTaskId =
      engagementId === 7
        ? new Map(rows.map((item) => [item.task_id, item]))
        : new Map<number, ReportingInputTaskRow>();

    return {
      engagementId,
      inputByTaskId,
      hasInventory: inputByTaskId.size > 0,
      isLoading: false,
      isError: false,
      error: null,
    };
  });

  render(<TaskPanel />);
  fireEvent.click(screen.getByRole("button", { name: "Client Alpha" }));
}

describe("TaskPanel reporting prepare shortcut", () => {
  beforeEach(() => {
    if (
      typeof window !== "undefined" &&
      window.localStorage &&
      typeof window.localStorage.clear === "function"
    ) {
      window.localStorage.clear();
    }
    mockedTasks = [task(101, "Recon"), task(102, "Validation")];
    toastSpy = vi.fn();
    reportingMocks.prepareMutateAsync = vi.fn().mockResolvedValue({});
    reportingMocks.prepareIsPending = false;
    reportingMocks.prepareVariables = undefined;
    reportingMocks.useTaskPanelReportingStatusProjection.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("prepares eligible task input with task engagement and regeneration state", () => {
    renderExpandedPanel([row(101, "stale"), row(102, "ready")]);

    fireEvent.click(screen.getByRole("button", { name: "Prepare" }));

    expect(reportingMocks.prepareMutateAsync).toHaveBeenCalledWith({
      task_id: 101,
      engagement_id: 7,
      regenerate: true,
    });
  });

  it("disables only the task card with a pending prepare action", () => {
    reportingMocks.prepareIsPending = true;
    reportingMocks.prepareVariables = { task_id: 101 };

    renderExpandedPanel([row(101, "stale"), row(102, "failed")]);

    const pendingButton = screen.getByRole("button", { name: "Preparing..." }) as HTMLButtonElement;
    const otherButton = screen.getByRole("button", { name: "Prepare" }) as HTMLButtonElement;

    expect(pendingButton.disabled).toBe(true);
    expect(otherButton.disabled).toBe(false);
  });

  it("shows backend error summary when prepare fails", async () => {
    reportingMocks.prepareMutateAsync = vi.fn().mockRejectedValue(new Error("Memo source is unavailable"));

    renderExpandedPanel([row(101, "not_prepared")]);
    fireEvent.click(screen.getByRole("button", { name: "Prepare" }));

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith({
        title: "Prepare failed",
        description: "Memo source is unavailable",
        variant: "destructive",
      });
    });
  });

  it("does not infer preparability from stopped task status when inventory says no", () => {
    renderExpandedPanel([
      row(101, "stale", {
        is_preparable: false,
        not_preparable_reason: "Runtime has not retired yet.",
        runtime_retired: false,
      }),
    ]);

    expect(screen.getByText("Report input:")).toBeTruthy();
    expect(screen.getByText("Stale")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Prepare" })).toBeNull();
  });
});
