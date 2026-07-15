// @vitest-environment jsdom
/* Tests for the report job progress panel rendering. */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReportJobProgress } from "@/components/reporting/report-job-progress";
import type { EngagementReportJobStatusResponse } from "@/types/reporting";

function job(
  overrides: Partial<EngagementReportJobStatusResponse> = {},
): EngagementReportJobStatusResponse {
  return {
    id: "job-1",
    engagement_id: 7,
    report_id: null,
    report_type: "pentest",
    status: "generating",
    selected_task_memo_ids: ["memo-1"],
    include_candidate_findings: false,
    source_watermark: {},
    generation_phase: "sections",
    current_section_id: "findings",
    completed_sections: ["scope"],
    total_sections: 4,
    attempt_count: 2,
    max_attempts: 3,
    last_error_code: null,
    error_message: null,
    next_attempt_at: null,
    last_error_at: null,
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:01:00Z",
    started_at: "2026-06-11T10:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

describe("<ReportJobProgress />", () => {
  it("renders typed active job progress fields", () => {
    render(<ReportJobProgress job={job()} />);

    expect(screen.getByText("Generating · 1 of 4")).toBeTruthy();
    expect(screen.getByText("findings")).toBeTruthy();
    expect(screen.getByText("1 of 4 completed")).toBeTruthy();
    expect(screen.getByText("2 of 3")).toBeTruthy();
  });

  it("renders an initial job as queued", () => {
    render(
      <ReportJobProgress
        job={job({
          status: "queued",
          current_section_id: null,
          completed_sections: [],
          attempt_count: 0,
          started_at: null,
        })}
      />,
    );

    expect(screen.getByText("Queued")).toBeTruthy();
  });

  it("renders durable finalization progress", () => {
    render(
      <ReportJobProgress
        job={job({
          generation_phase: "finalizing",
          current_section_id: null,
          completed_sections: ["1", "2", "3", "4", "5", "6", "7"],
          total_sections: 7,
        })}
      />,
    );

    expect(screen.getByText("Finalizing · 7 of 7")).toBeTruthy();
  });

  it("keeps durable progress visible while a section retry is scheduled", () => {
    render(
      <ReportJobProgress
        job={job({
          status: "queued",
          current_section_id: "vulnerability_summary",
          completed_sections: ["scope", "methodology", "executive_summary"],
          total_sections: 7,
          attempt_count: 1,
          next_attempt_at: "2026-06-11T10:01:05Z",
          last_error_at: "2026-06-11T10:01:00Z",
          failure_details: {
            failed_section_id: "vulnerability_summary",
            failed_section_order: 4,
            failed_section_type: "findings",
            validation_issues: [],
          },
        })}
      />,
    );

    expect(screen.getByText("Retrying section 4 · attempt 2 of 3")).toBeTruthy();
    expect(screen.getByText("3 of 7 completed")).toBeTruthy();
  });

  it("shows finalization retry without regressing completed progress", () => {
    render(
      <ReportJobProgress
        job={job({
          status: "queued",
          generation_phase: "finalizing",
          current_section_id: null,
          completed_sections: ["1", "2", "3", "4", "5", "6", "7"],
          total_sections: 7,
          attempt_count: 1,
          next_attempt_at: "2026-06-11T10:01:05Z",
          last_error_at: "2026-06-11T10:01:00Z",
        })}
      />,
    );

    expect(screen.getByText("Retrying finalization · attempt 2 of 3")).toBeTruthy();
    expect(screen.getAllByText("7 of 7 completed").length).toBeGreaterThan(0);
  });

  it("renders failed backend errors and retry guidance", () => {
    render(
      <ReportJobProgress
        job={job({
          status: "failed",
          error_message: "Report source validation failed.",
          failure_details: {
            failed_section_id: "vulnerability_summary",
            failed_section_order: 4,
            failed_section_type: "findings",
            validation_issues: [
              {
                code: "transcript_only_reportable_content",
                path: "source_refs",
              },
            ],
          },
          finished_at: "2026-06-11T10:02:00Z",
        })}
      />,
    );

    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText("Report source validation failed.")).toBeTruthy();
    expect(screen.getByText("Section 4: vulnerability_summary (findings)")).toBeTruthy();
    expect(screen.getByText("transcript_only_reportable_content at source_refs")).toBeTruthy();
    expect(screen.getByText(/Selected inputs remain selected/)).toBeTruthy();
  });

  it("renders non-blocking empty and loading states", () => {
    const { container, rerender } = render(<ReportJobProgress job={null} />);
    expect(container.firstChild).toBeNull();

    rerender(<ReportJobProgress job={null} isLoading />);
    expect(screen.getByText("Loading report progress...")).toBeTruthy();
  });

  it("renders a visible submitting state before a job id is available", () => {
    render(<ReportJobProgress job={null} isSubmitting />);

    expect(screen.getByText("Submitting")).toBeTruthy();
    expect(screen.getByText("Submitting report generation request...")).toBeTruthy();
  });
});
