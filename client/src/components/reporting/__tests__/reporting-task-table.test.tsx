// @vitest-environment jsdom
/* Tests for the engagement reporting input task table. */

import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  getReportingTaskSelectionState,
  ReportingTaskTable,
} from "@/components/reporting/reporting-task-table";
import type {
  ReportingInputState,
  ReportingInputTaskRow,
  TaskClosureMemoSummary,
} from "@/types/reporting";

function sourceWatermark() {
  return {
    last_chat_message_id: null,
    last_turn_sequence: null,
    latest_tool_execution_id: null,
    latest_evidence_created_at: null,
    latest_knowledge_observed_at: null,
  };
}

function memo(
  id: string,
  overrides: Partial<TaskClosureMemoSummary> = {},
): TaskClosureMemoSummary {
  return {
    id,
    version: 2,
    status: "ready",
    memo_mode: "supported",
    is_current: true,
    source_watermark: sourceWatermark(),
    error_message: null,
    created_at: "2026-06-10T10:00:00Z",
    updated_at: "2026-06-10T10:15:00Z",
    generated_at: "2026-06-10T10:15:00Z",
    ...overrides,
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
    current_memo: inputState === "ready" ? memo(`memo-${taskId}`) : null,
    latest_memo_attempt: null,
    source_watermark: sourceWatermark(),
    counts: {
      evidence: taskId,
      canonical_findings: taskId + 1,
      candidate_findings: taskId + 2,
    },
    candidate_findings_require_explicit_inclusion: true,
    ...overrides,
  };
}

function renderTable(
  tasks: ReportingInputTaskRow[],
  options: {
    selectedTaskIds?: Set<number>;
    onSelectedTaskIdsChange?: (selectedTaskIds: Set<number>) => void;
    onPrepareTask?: ComponentProps<typeof ReportingTaskTable>["onPrepareTask"];
    preparingTaskIds?: Set<number>;
  } = {},
) {
  const onSelectedTaskIdsChange = vi.fn<(selectedTaskIds: Set<number>) => void>();
  const handleSelectedTaskIdsChange =
    options.onSelectedTaskIdsChange ?? onSelectedTaskIdsChange;
  render(
    <ReportingTaskTable
      tasks={tasks}
      selectedTaskIds={options.selectedTaskIds ?? new Set()}
      onSelectedTaskIdsChange={handleSelectedTaskIdsChange}
      onPrepareTask={options.onPrepareTask}
      preparingTaskIds={options.preparingTaskIds}
    />,
  );
  return { onSelectedTaskIdsChange };
}

describe("<ReportingTaskTable />", () => {
  it("renders inventory fields and keeps long task names constrained", () => {
    const longName =
      "External perimeter validation task with a very long customer-facing name";
    renderTable([
      row(7, "ready", {
        task_name: longName,
        counts: {
          evidence: 12,
          canonical_findings: 3,
          candidate_findings: 5,
        },
      }),
    ]);

    expect(screen.getByText(longName).className).toContain("truncate");
    expect(screen.getByText(longName).getAttribute("title")).toBe(longName);
    expect(screen.getByText("Stopped")).toBeTruthy();
    expect(screen.queryByLabelText("Reporting input status: Ready")).toBeNull();
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.getByText("v2")).toBeTruthy();
    expect(screen.getByText(/Jun 10/)).toBeTruthy();
  });

  it("selects only rows eligible for generation or preparation", () => {
    const ready = row(1, "ready", { current_memo: memo("memo-ready") });
    const preparable = row(2, "not_prepared");
    const running = row(3, "not_prepared", {
      task_status: "running",
      runtime_retired: false,
      is_preparable: false,
      not_preparable_reason: "task_not_stopped",
    });
    const readyWithoutMemo = row(4, "ready", { current_memo: null });
    const { onSelectedTaskIdsChange } = renderTable([
      ready,
      preparable,
      running,
      readyWithoutMemo,
    ]);

    expect(getReportingTaskSelectionState(ready)).toMatchObject({
      selectable: true,
      purpose: "generation",
    });
    expect(getReportingTaskSelectionState(preparable)).toMatchObject({
      selectable: true,
      purpose: "preparation",
    });
    expect(screen.getByText("Stop this task before preparing input.")).toBeTruthy();
    expect(screen.getByText("Ready input is missing a current memo.")).toBeTruthy();

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 1" }));
    expect([...onSelectedTaskIdsChange.mock.calls[0][0]]).toEqual([1]);

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 2" }));
    expect([...onSelectedTaskIdsChange.mock.calls[1][0]]).toEqual([2]);

    const disabledCheckbox = screen.getByRole("checkbox", { name: "Select Task 3" });
    expect((disabledCheckbox as HTMLButtonElement).disabled).toBe(true);
  });

  it("surfaces failed memo errors and row prepare actions without batch behavior", () => {
    const onPrepareTask = vi.fn();
    renderTable(
      [
        row(8, "failed", {
          task_name: "Failed memo task",
          latest_memo_attempt: memo("failed-attempt", {
            status: "failed",
            error_message: "Summarization exceeded the available source window.",
            is_current: false,
          }),
        }),
        row(9, "not_prepared", { task_name: "Missing memo task" }),
        row(10, "preparing", { task_name: "Preparing memo task" }),
      ],
      { onPrepareTask, preparingTaskIds: new Set([10]) },
    );

    expect(
      screen.getByText("Summarization exceeded the available source window."),
    ).toBeTruthy();

    const failedRow = screen.getByText("Failed memo task").closest("tr");
    const missingRow = screen.getByText("Missing memo task").closest("tr");
    const preparingRow = screen.getByText("Preparing memo task").closest("tr");
    expect(failedRow).toBeTruthy();
    expect(missingRow).toBeTruthy();
    expect(preparingRow).toBeTruthy();

    fireEvent.click(within(failedRow as HTMLTableRowElement).getByRole("button"));
    expect(onPrepareTask).toHaveBeenCalledWith(expect.objectContaining({ task_id: 8 }), {
      regenerate: true,
    });

    fireEvent.click(within(missingRow as HTMLTableRowElement).getByRole("button"));
    expect(onPrepareTask).toHaveBeenCalledWith(expect.objectContaining({ task_id: 9 }), {
      regenerate: false,
    });

    expect(
      (within(preparingRow as HTMLTableRowElement).getByRole(
        "button",
      ) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});
