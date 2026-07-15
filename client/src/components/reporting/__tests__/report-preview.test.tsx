// @vitest-environment jsdom
/* Tests for the safe current report preview panel. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReportPreview } from "@/components/reporting/report-preview";
import type { EngagementReportReadResponse } from "@/types/reporting";

function report(
  overrides: Partial<EngagementReportReadResponse> = {},
): EngagementReportReadResponse {
  return {
    id: "report-1",
    schema_version: "engagement_report.v1",
    engagement_id: 7,
    report_type: "pentest",
    version: 2,
    status: "ready",
    is_current: true,
    title: "Pentest Report",
    sections: [],
    markdown_snapshot: "# Pentest Report\n\n<img src=x onerror=alert(1)>",
    source_task_memo_ids: ["memo-1", "memo-2"],
    source_knowledge_refs: [
      {
        ref: "knowledge-1",
        task_id: 41,
        record_type: "note",
        authoritative: true,
      },
    ],
    source_evidence_refs: [
      {
        ref: "evidence-1",
        task_id: 41,
        evidence_type: "screenshot",
        source_tool: "browser",
      },
    ],
    generation_metadata: null,
    error_message: null,
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:05:00Z",
    generated_at: "2026-06-11T10:05:00Z",
    ...overrides,
  };
}

describe("<ReportPreview />", () => {
  beforeEach(() => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:report-markdown");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders markdown snapshots as safe rendered markdown", () => {
    const { container } = render(<ReportPreview report={report()} />);

    expect(
      screen.getByRole("heading", { name: "Engagement Report Preview" }),
    ).toBeTruthy();
    expect(screen.queryByText("Current Report")).toBeNull();
    expect(screen.getByRole("heading", { name: "Engagement Report" })).toBeTruthy();
    expect(screen.getByText("Version 2")).toBeTruthy();
    expect(screen.queryByText("Ready")).toBeNull();
    expect(screen.getByText("2 tasks")).toBeTruthy();
    expect(screen.getByText("1 evidence")).toBeTruthy();
    expect(screen.getByText("1 knowledge")).toBeTruthy();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(container.querySelector("img")).toBeNull();
  });

  it("downloads the rendered markdown snapshot", () => {
    const appendChild = vi.spyOn(document.body, "appendChild");
    const removeChild = vi.spyOn(document.body, "removeChild");

    render(<ReportPreview report={report()} />);

    fireEvent.click(screen.getByRole("button", { name: /download/i }));

    expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:report-markdown");

    const link = appendChild.mock.calls.find(
      ([element]) => element instanceof HTMLAnchorElement,
    )?.[0] as HTMLAnchorElement | undefined;
    expect(link?.download).toBe("engagement-report-v2.md");
    expect(link?.href).toBe("blob:report-markdown");
    expect(removeChild).toHaveBeenCalledWith(link);
  });

  it("falls back to structured sections when no markdown snapshot exists", () => {
    const { container } = render(
      <ReportPreview
        report={report({
          markdown_snapshot: null,
          sections: [
            {
              schema_version: "engagement_report_section.v1",
              section_id: "scope",
              section_type: "summary",
              title: "Scope",
              status: "ready",
              content_markdown: "Assessment covered the public API.",
              blocks: [
                {
                  block_id: "finding-1",
                  block_type: "finding",
                  title: "Missing rate limits",
                  severity: "medium",
                  confidence: "high",
                  affected_assets: ["api.example.test"],
                  content_markdown: "The API accepted repeated attempts.",
                  impact_markdown: "Credential attacks are easier.",
                  remediation_markdown: "Add rate limits.",
                  source_refs: {
                    task_memo_ids: ["memo-1"],
                    knowledge_refs: [],
                    evidence_refs: ["evidence-1"],
                  },
                },
              ],
              source_refs: {
                task_memo_ids: ["memo-1"],
                knowledge_refs: [],
                evidence_refs: ["evidence-1"],
              },
              unsupported_notes: [],
              generation_notes: [],
            },
          ],
        })}
      />,
    );

    expect(screen.getByRole("heading", { name: "Scope" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Missing rate limits" })).toBeTruthy();
    expect(container.textContent).toContain("Remediation");
    expect(container.textContent).toContain("Add rate limits.");
  });

  it("distinguishes loading, error, empty, and content sizing states", () => {
    const { rerender } = render(<ReportPreview report={null} isLoading />);
    expect(screen.getByText("Loading current report...")).toBeTruthy();

    rerender(
      <ReportPreview report={null} isError errorMessage="Current report unavailable." />,
    );
    expect(screen.getByText("Current report unavailable.")).toBeTruthy();

    rerender(<ReportPreview report={null} />);
    expect(screen.getByText("No current report for this type.")).toBeTruthy();

    rerender(
      <ReportPreview report={report({ markdown_snapshot: "Long report body" })} />,
    );
    const previewRegion = screen.getByText("Long report body").closest("article");
    expect(previewRegion?.className.includes("overflow-y-auto")).toBe(true);
    expect(previewRegion?.className.includes("lg:max-h-[calc(100vh-18rem)]")).toBe(
      true,
    );
  });

  it("exposes current report deletion action", () => {
    const onDeleteReport = vi.fn();
    const currentReport = report();
    render(<ReportPreview report={currentReport} onDeleteReport={onDeleteReport} />);

    fireEvent.click(screen.getByRole("button", { name: /delete report/i }));

    expect(onDeleteReport).toHaveBeenCalledWith(currentReport);
  });
});
