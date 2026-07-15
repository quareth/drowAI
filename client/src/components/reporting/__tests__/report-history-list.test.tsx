// @vitest-environment jsdom
/* Tests for historical engagement report list rendering and preview actions. */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ReportHistoryList } from "@/components/reporting/report-history-list";
import type { EngagementReportHistoryItem } from "@/types/reporting";

function historyItem(
  overrides: Partial<EngagementReportHistoryItem> = {},
): EngagementReportHistoryItem {
  return {
    report_id: "report-1",
    engagement_id: 7,
    report_type: "pentest",
    version: 2,
    status: "ready",
    is_current: false,
    title: "Pentest Report",
    source_task_memo_ids: ["memo-1", "memo-2"],
    source_knowledge_refs: [],
    source_evidence_refs: [],
    generation_metadata: null,
    error_message: null,
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:05:00Z",
    generated_at: "2026-06-11T10:05:00Z",
    ...overrides,
  };
}

describe("<ReportHistoryList />", () => {
  it("renders loading, error, and empty states", () => {
    const { rerender } = render(
      <ReportHistoryList reports={[]} isLoading onOpenReport={vi.fn()} />,
    );

    expect(screen.getByText("Loading report history...")).toBeTruthy();

    rerender(
      <ReportHistoryList
        reports={[]}
        isError
        errorMessage="History unavailable."
        onOpenReport={vi.fn()}
      />,
    );
    expect(screen.getByText("History unavailable.")).toBeTruthy();

    rerender(<ReportHistoryList reports={[]} onOpenReport={vi.fn()} />);
    expect(screen.getByText("No previous reports.")).toBeTruthy();
  });

  it("omits the current report and opens a previous report for preview", () => {
    const onOpenReport = vi.fn();
    render(
      <ReportHistoryList
        reports={[
          historyItem({ report_id: "report-current", is_current: true }),
          historyItem({
            report_id: "report-old",
            version: 1,
            title: "Previous Client Alpha Report",
          }),
        ]}
        selectedReportId="report-old"
        onOpenReport={onOpenReport}
      />,
    );

    expect(screen.queryByText("Current")).toBeNull();
    expect(screen.getByText("1 previous report.")).toBeTruthy();
    expect(screen.getByText("2 tasks")).toBeTruthy();
    expect(screen.queryByText("Ready")).toBeNull();
    expect(
      (screen.getByRole("button", { name: /Open report Previous/ }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);

    expect(
      screen.queryByRole("button", { name: "Open report Engagement Report version 2" }),
    ).toBeNull();

    expect(onOpenReport).not.toHaveBeenCalled();
  });

  it("renders failed report summaries without raw structured payloads", () => {
    render(
      <ReportHistoryList
        reports={[
          historyItem({
            report_id: "report-failed",
            status: "failed",
            error_message: '{"token":"secret"}',
          }),
        ]}
        onOpenReport={vi.fn()}
      />,
    );

    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText("Report generation failed.")).toBeTruthy();
    expect(screen.queryByText(/secret/)).toBeNull();
  });

  it("exposes delete actions for historical reports", () => {
    const onDeleteReport = vi.fn();
    render(
      <ReportHistoryList
        reports={[historyItem({ report_id: "report-old", version: 1 })]}
        onOpenReport={vi.fn()}
        onDeleteReport={onDeleteReport}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Delete report/ }));

    expect(onDeleteReport).toHaveBeenCalledWith(
      expect.objectContaining({ report_id: "report-old" }),
    );
  });
});
