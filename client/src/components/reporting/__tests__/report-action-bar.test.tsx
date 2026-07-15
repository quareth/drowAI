// @vitest-environment jsdom
/* Tests for selected reporting input action controls. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReportActionBar } from "@/components/reporting/report-action-bar";
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

function memo(id: string): TaskClosureMemoSummary {
  return {
    id,
    version: 1,
    status: "ready",
    memo_mode: "supported",
    is_current: true,
    source_watermark: sourceWatermark(),
    error_message: null,
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    generated_at: "2026-06-11T10:00:00Z",
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
    source_watermark: sourceWatermark(),
    counts: {
      evidence: 1,
      canonical_findings: 1,
      candidate_findings: 0,
    },
    candidate_findings_require_explicit_inclusion: false,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("<ReportActionBar />", () => {
  it("enables Prepare Selected only when a selected row is preparable", () => {
    const onPrepareSelected = vi.fn();
    const rows = [row(1, "ready"), row(2, "not_prepared")];
    const { rerender } = render(
      <ReportActionBar
        rows={rows}
        selectedTaskIds={new Set([1])}
        selectedEngagementId={7}
        reportType="pentest"
        onPrepareSelected={onPrepareSelected}
        onGenerateReport={vi.fn()}
      />,
    );

    const disabledButton = screen.getByRole("button", {
      name: "Prepare Selected",
    }) as HTMLButtonElement;
    expect(disabledButton.disabled).toBe(true);
    expect(screen.getByText("No selected inputs need preparation.")).toBeTruthy();

    rerender(
      <ReportActionBar
        rows={rows}
        selectedTaskIds={new Set([2])}
        selectedEngagementId={7}
        reportType="pentest"
        onPrepareSelected={onPrepareSelected}
        onGenerateReport={vi.fn()}
      />,
    );

    const enabledButton = screen.getByRole("button", {
      name: "Prepare Selected",
    }) as HTMLButtonElement;
    expect(enabledButton.disabled).toBe(false);
    fireEvent.click(enabledButton);
    expect(onPrepareSelected).toHaveBeenCalledTimes(1);
  });

  it("shows aggregate preparation progress and retryable failures", () => {
    render(
      <ReportActionBar
        rows={[row(2, "failed"), row(3, "stale")]}
        selectedTaskIds={new Set([2, 3])}
        selectedEngagementId={7}
        reportType="pentest"
        isPreparingSelected
        prepareProgress={{
          total: 2,
          completed: 1,
          failed: 1,
          inFlightTaskIds: [3],
        }}
        prepareResults={[
          {
            task_id: 2,
            task_name: "Task 2",
            regenerate: true,
            ok: false,
            error_message: "Permission denied",
          },
        ]}
        onPrepareSelected={vi.fn()}
        onGenerateReport={vi.fn()}
      />,
    );

    expect(screen.getByText("Preparing 1 of 2 selected inputs...")).toBeTruthy();
    expect(screen.getByText("Some inputs need another preparation attempt.")).toBeTruthy();
    expect(screen.getByText(/Permission denied/)).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Prepare Selected" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("updates action validation summary from engagement, selection, and inventory", () => {
    const rows = [row(1, "stale"), row(2, "ready", { current_memo: memo("memo-2") })];
    const { rerender } = render(
      <ReportActionBar
        rows={rows}
        selectedTaskIds={new Set([1])}
        selectedEngagementId={null}
        reportType="pentest"
        onPrepareSelected={vi.fn()}
        onGenerateReport={vi.fn()}
      />,
    );

    expect(screen.getByText("Select an engagement to prepare inputs.")).toBeTruthy();
    expect(screen.getByText("Select an engagement to generate a report.")).toBeTruthy();

    rerender(
      <ReportActionBar
        rows={rows}
        selectedTaskIds={new Set([1])}
        selectedEngagementId={7}
        reportType="pentest"
        onPrepareSelected={vi.fn()}
        onGenerateReport={vi.fn()}
      />,
    );

    expect(
      screen.getByText(
        "Selected input is stale. Regenerate it before generating a report.",
      ),
    ).toBeTruthy();

    rerender(
      <ReportActionBar
        rows={rows}
        selectedTaskIds={new Set([2])}
        selectedEngagementId={7}
        reportType="pentest"
        onPrepareSelected={vi.fn()}
        onGenerateReport={vi.fn()}
      />,
    );

    expect(screen.getByText("1 ready input can generate a report.")).toBeTruthy();
  });

  it("keeps backend action errors visible without raw payload dumps", () => {
    const { rerender } = render(
      <ReportActionBar
        rows={[row(2, "failed")]}
        selectedTaskIds={new Set([2])}
        selectedEngagementId={7}
        reportType="pentest"
        prepareResults={[
          {
            task_id: 2,
            task_name: "Task 2",
            regenerate: true,
            ok: false,
            error_message: "Permission denied",
          },
        ]}
        onPrepareSelected={vi.fn()}
        onGenerateReport={vi.fn()}
      />,
    );

    expect(screen.getByText(/Permission denied/)).toBeTruthy();

    rerender(
      <ReportActionBar
        rows={[row(2, "failed")]}
        selectedTaskIds={new Set([2])}
        selectedEngagementId={7}
        reportType="pentest"
        prepareResults={[
          {
            task_id: 2,
            task_name: "Task 2",
            regenerate: true,
            ok: false,
            error_message: '{"detail":"token=abc123"}',
          },
        ]}
        onPrepareSelected={vi.fn()}
        onGenerateReport={vi.fn()}
      />,
    );

    expect(
      screen.getByText(
        /Backend rejected the action. Review the selected inputs and permissions./,
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/token=abc123/)).toBeNull();
  });

  it("enables Generate Report only for ready current memo selections", () => {
    const onGenerateReport = vi.fn();
    const rows = [
      row(1, "ready", { current_memo: memo("memo-1") }),
      row(2, "stale", { current_memo: memo("memo-2") }),
    ];
    const { rerender } = render(
      <ReportActionBar
        rows={rows}
        selectedTaskIds={new Set([2])}
        selectedEngagementId={7}
        reportType="pentest"
        onPrepareSelected={vi.fn()}
        onGenerateReport={onGenerateReport}
      />,
    );

    const disabledButton = screen.getByRole("button", {
      name: "Generate Report",
    }) as HTMLButtonElement;
    expect(disabledButton.disabled).toBe(true);
    expect(
      screen.getByText(
        "Selected input is stale. Regenerate it before generating a report.",
      ),
    ).toBeTruthy();

    rerender(
      <ReportActionBar
        rows={rows}
        selectedTaskIds={new Set([1])}
        selectedEngagementId={7}
        reportType="pentest"
        onPrepareSelected={vi.fn()}
        onGenerateReport={onGenerateReport}
      />,
    );

    const enabledButton = screen.getByRole("button", {
      name: "Generate Report",
    }) as HTMLButtonElement;
    expect(enabledButton.disabled).toBe(false);
    fireEvent.click(enabledButton);
    expect(onGenerateReport).toHaveBeenCalledTimes(1);
  });

  it("labels generation as a new report version when a current report exists", () => {
    const onGenerateReport = vi.fn();
    render(
      <ReportActionBar
        rows={[row(1, "ready", { current_memo: memo("memo-1") })]}
        selectedTaskIds={new Set([1])}
        selectedEngagementId={7}
        reportType="pentest"
        hasCurrentReport
        onPrepareSelected={vi.fn()}
        onGenerateReport={onGenerateReport}
      />,
    );

    expect(screen.getByText("1 ready input can generate a new report version.")).toBeTruthy();
    const button = screen.getByRole("button", {
      name: "Generate New Report",
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    expect(onGenerateReport).toHaveBeenCalledWith(["memo-1"]);
  });
});
