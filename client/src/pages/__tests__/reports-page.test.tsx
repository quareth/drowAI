/**
 * Reports page shell tests.
 *
 * Responsibility: verify `/reports` mounts the engagement reporting workspace
 * inside app chrome without reaching the legacy task-owned reports API.
 */
// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReportsPage from "@/pages/reports-page";
import type {
  EngagementReportHistoryItem,
  EngagementReportJobStatusResponse,
  EngagementReportReadResponse,
  ReportLibraryItem,
  ReportingInputState,
  ReportingInputTaskRow,
  TaskClosureMemoSummary,
} from "@/types/reporting";

const mocked = vi.hoisted(() => ({
  apiCall: vi.fn(),
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiCall: mocked.apiCall,
  apiFetch: mocked.apiFetch,
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: { actions: ["report.write", "report.delete"] },
  }),
}));

vi.mock("@/components/layout/navbar", () => ({
  Navbar: () => <div data-testid="navbar">navbar</div>,
}));

vi.mock("@/components/layout/sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar">sidebar</div>,
}));

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function engagementItem(id: number, name: string) {
  return {
    id,
    user_id: 1,
    name,
    description: null,
    status: "active",
    metadata: {},
    created_at: null,
    updated_at: null,
  };
}

function sourceWatermark() {
  return {
    last_chat_message_id: null,
    last_turn_sequence: null,
    latest_tool_execution_id: null,
    latest_evidence_created_at: null,
    latest_knowledge_observed_at: null,
  };
}

function memoSummary(id: string): TaskClosureMemoSummary {
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

function reportingRow(
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

function reportResponse(
  overrides: Partial<EngagementReportReadResponse> = {},
): EngagementReportReadResponse {
  return {
    id: "report-current",
    schema_version: "engagement_report.v1",
    engagement_id: 7,
    report_type: "pentest",
    version: 2,
    status: "ready",
    is_current: true,
    engagement_name_snapshot: "Client Alpha",
    engagement_status_snapshot: "active",
    title: "Client Alpha Generated Report",
    sections: [],
    markdown_snapshot: "## Executive Summary\n\nGenerated report body.",
    source_task_memo_ids: ["memo-43"],
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

function historyItem(
  overrides: Partial<EngagementReportHistoryItem> = {},
): EngagementReportHistoryItem {
  return {
    report_id: "report-current",
    engagement_id: 7,
    report_type: "pentest",
    version: 2,
    status: "ready",
    is_current: true,
    engagement_name_snapshot: "Client Alpha",
    engagement_status_snapshot: "active",
    title: "Client Alpha Generated Report",
    source_task_memo_ids: ["memo-43"],
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

function jobStatus(
  overrides: Partial<EngagementReportJobStatusResponse> = {},
): EngagementReportJobStatusResponse {
  return {
    id: "job-7",
    engagement_id: 7,
    report_id: "report-current",
    report_type: "pentest",
    status: "ready",
    selected_task_memo_ids: ["memo-43"],
    include_candidate_findings: false,
    source_watermark: {},
    generation_phase: "finalizing",
    current_section_id: null,
    completed_sections: ["scope", "findings", "recommendations"],
    total_sections: 3,
    attempt_count: 1,
    max_attempts: 3,
    last_error_code: null,
    error_message: null,
    next_attempt_at: null,
    last_error_at: null,
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:05:00Z",
    started_at: "2026-06-11T10:00:00Z",
    finished_at: "2026-06-11T10:05:00Z",
    ...overrides,
  };
}

function libraryItem(overrides: Partial<ReportLibraryItem> = {}): ReportLibraryItem {
  return {
    report_id: "report-library",
    engagement_id: 7,
    engagement_name_snapshot: "Deleted Client Alpha",
    engagement_status_snapshot: "active",
    report_type: "pentest",
    version: 1,
    status: "ready",
    is_current: true,
    title: "Deleted Engagement Report",
    source_task_count: 2,
    source_knowledge_count: 3,
    source_evidence_count: 4,
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:05:00Z",
    generated_at: "2026-06-11T10:05:00Z",
    ...overrides,
  };
}

let reportingTasks: ReportingInputTaskRow[] = [];
let currentReport: EngagementReportReadResponse | null = null;
let reportHistory: EngagementReportHistoryItem[] = [];
let reportLibrary: ReportLibraryItem[] = [];

function responseForUrl(url: string, init?: RequestInit): Response {
  if (url.startsWith("/api/engagements?")) {
    return jsonResponse({
      items: [engagementItem(7, "Client Alpha")],
      total: 1,
      limit: 100,
      offset: 0,
    });
  }
  if (url === "/api/llm/reporting-selection") {
    return jsonResponse({
      provider: "openai",
      model: "gpt-5.2",
      reasoning_effort: "medium",
      selection_status: {
        runnable: true,
        reason: null,
      },
    });
  }
  if (url === "/api/reporting/engagements/7/inputs") {
    return jsonResponse({ engagement_id: 7, tasks: reportingTasks });
  }
  if (url.includes("/memo/prepare")) {
    const taskId = Number(url.match(/tasks\/(\d+)/)?.[1] ?? 0);
    return jsonResponse({
      task_id: taskId,
      memo: {
        id: `memo-${taskId}`,
        schema_version: "task_closure_memo.v1",
        engagement_id: 7,
        task_id: taskId,
        version: 1,
        status: "ready",
        memo_mode: "supported",
        is_current: true,
        source_watermark: sourceWatermark(),
        error_message: null,
        created_at: "2026-06-11T10:00:00Z",
        updated_at: "2026-06-11T10:00:00Z",
        generated_at: "2026-06-11T10:00:00Z",
        body: null,
      },
    });
  }
  if (url === "/api/reporting/engagements/7/reports" && init?.method === "POST") {
    return jsonResponse({ job_id: "job-7", report_id: null, status: "queued" });
  }
  if (url.startsWith("/api/reporting/reports?")) {
    const params = new URL(url, "http://localhost").searchParams;
    const limit = Number(params.get("limit") ?? 50);
    const offset = Number(params.get("offset") ?? 0);
    return jsonResponse({
      reports: reportLibrary.slice(offset, offset + limit),
      total: reportLibrary.length,
      limit,
      offset,
    });
  }
  if (url === "/api/reporting/jobs/job-7") {
    currentReport = reportResponse();
    reportHistory = [
      historyItem(),
      historyItem({
        report_id: "report-old",
        version: 1,
        is_current: false,
        title: "Client Alpha Earlier Report",
        generated_at: "2026-06-10T10:00:00Z",
      }),
    ];
    return jsonResponse(jobStatus());
  }
  if (url === "/api/reporting/reports/report-library") {
    return jsonResponse(
      reportResponse({
        id: "report-library",
        version: 1,
        is_current: true,
        engagement_name_snapshot: "Deleted Client Alpha",
        title: "Deleted Engagement Report",
        markdown_snapshot: "## Historical Library\n\nDurable report body.",
      }),
    );
  }
  if (url === "/api/reporting/reports/report-current") {
    return jsonResponse(reportResponse());
  }
  if (url === "/api/reporting/reports/report-old") {
    return jsonResponse(
      reportResponse({
        id: "report-old",
        version: 1,
        is_current: false,
        title: "Client Alpha Earlier Report",
        markdown_snapshot: "## Earlier Summary\n\nHistorical report body.",
        generated_at: "2026-06-10T10:00:00Z",
      }),
    );
  }
  if (url.includes("/reports/current")) {
    return jsonResponse({
      engagement_id: 7,
      report_type: "pentest",
      report: currentReport,
    });
  }
  if (url.includes("/reports/history")) {
    return jsonResponse({
      engagement_id: 7,
      report_type: "pentest",
      reports: reportHistory,
    });
  }
  return jsonResponse({ detail: "Unexpected test URL" });
}

function renderReportsPage() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  render(
    <QueryClientProvider client={client}>
      <ReportsPage />
    </QueryClientProvider>,
  );
  return client;
}

function calledUrls(): string[] {
  return mocked.apiFetch.mock.calls.map(([url]) => String(url));
}

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

describe("<ReportsPage />", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/reports");
    globalThis.ResizeObserver = TestResizeObserver;
    Element.prototype.scrollIntoView = vi.fn();
    reportingTasks = [];
    currentReport = null;
    reportHistory = [];
    reportLibrary = [];
    mocked.apiFetch.mockImplementation((url: string, init?: RequestInit) =>
      Promise.resolve(responseForUrl(String(url), init)),
    );
    mocked.apiCall.mockImplementation(async (url: string, init?: RequestInit) => {
      const response = responseForUrl(String(url), init);
      if (!response.ok) {
        throw new Error(await response.text());
      }
      return response.json();
    });
  });

  afterEach(() => {
    cleanup();
    mocked.apiCall.mockReset();
    mocked.apiFetch.mockReset();
    vi.restoreAllMocks();
  });

  it("renders the reporting workspace shell without calling legacy reports endpoints", async () => {
    renderReportsPage();

    expect(screen.getByTestId("navbar")).toBeTruthy();
    expect(screen.getByTestId("sidebar")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Library" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Engagement Report" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Report Library" })).toBeTruthy();

    await waitFor(() =>
      expect(calledUrls()).toContain(
        "/api/reporting/reports?report_type=pentest&limit=50&offset=0",
      ),
    );
    expect(calledUrls()).not.toContain("/api/engagements?limit=100");
    expect(calledUrls().some((url) => url.startsWith("/api/reports"))).toBe(false);
  });

  it("opens the engagement report tab from the tab query parameter", async () => {
    window.history.replaceState({}, "", "/reports?tab=engagement");

    renderReportsPage();

    expect(await screen.findByRole("heading", { name: "Select an engagement" })).toBeTruthy();
  });

  it("honors an explicit library tab query parameter", async () => {
    window.history.replaceState({}, "", "/reports?tab=library&engagement_id=7");

    renderReportsPage();

    expect(screen.getByRole("heading", { name: "Report Library" })).toBeTruthy();
  });

  it("opens and downloads a library report without selecting an engagement", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:report-library");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    reportLibrary = [libraryItem()];
    renderReportsPage();

    expect(await screen.findByText("Deleted Engagement Report")).toBeTruthy();
    expect(screen.getByText("Deleted Client Alpha")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Open report Deleted Engagement Report version 1",
      }),
    );

    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/reports/report-library"),
    );
    expect(await screen.findByRole("heading", { name: "Historical Library" })).toBeTruthy();
    expect(screen.getByText("Durable report body.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Download report" }));
    expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(calledUrls().some((url) => url.startsWith("/api/reports"))).toBe(false);
  });

  it("paginates the generated report library past the first 50 reports", async () => {
    reportLibrary = Array.from({ length: 60 }, (_, index) =>
      libraryItem({
        report_id: `report-library-${index + 1}`,
        title: `Library Report ${index + 1}`,
        version: index + 1,
      }),
    );
    renderReportsPage();

    expect(await screen.findByText("Library Report 1")).toBeTruthy();
    expect(screen.getByText("Showing 1-50 of 60 generated reports.")).toBeTruthy();
    expect(screen.queryByText("Library Report 51")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() =>
      expect(calledUrls()).toContain(
        "/api/reporting/reports?report_type=pentest&limit=50&offset=50",
      ),
    );
    expect(await screen.findByText("Library Report 51")).toBeTruthy();
    expect(screen.getByText("Showing 51-60 of 60 generated reports.")).toBeTruthy();
  });

  it("drives the reporting workflow through preparation, generation, preview, and history without legacy reports calls", async () => {
    reportingTasks = [
      reportingRow(41, "not_prepared"),
      reportingRow(42, "stale"),
      reportingRow(43, "ready", { current_memo: memoSummary("memo-43") }),
    ];
    const client = renderReportsPage();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    fireEvent.click(screen.getByRole("button", { name: "Engagement Report" }));
    expect(await screen.findByRole("heading", { name: "Select an engagement" })).toBeTruthy();
    expect(screen.queryByText("Generated reports remain available as tenant-owned artifacts.")).toBeNull();

    fireEvent.click(await screen.findByRole("combobox", { name: "Engagement" }));
    fireEvent.click(await screen.findByText("Client Alpha"));

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    expect(screen.getByText("Task 42")).toBeTruthy();
    expect(screen.getByText("Task 43")).toBeTruthy();

    await waitFor(() => {
      expect(
        (screen.getByRole("button", { name: "Prepare Task 41" }) as HTMLButtonElement)
          .disabled,
      ).toBe(false);
      expect(
        (screen.getByRole("button", { name: "Regenerate Task 42" }) as HTMLButtonElement)
          .disabled,
      ).toBe(false);
    });
    fireEvent.click(screen.getByRole("button", { name: "Prepare Task 41" }));
    fireEvent.click(screen.getByRole("button", { name: "Regenerate Task 42" }));

    await waitFor(() => {
      expect(calledUrls().filter((url) => url.includes("/memo/prepare"))).toHaveLength(2);
    });
    expect(calledUrls()).toEqual(
      expect.arrayContaining([
        "/api/reporting/tasks/41/memo/prepare",
        "/api/reporting/tasks/42/memo/prepare",
      ]),
    );

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 43" }));
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Generate Report" }));

    await waitFor(() => expect(calledUrls()).toContain("/api/reporting/jobs/job-7"));
    const generateCall = mocked.apiFetch.mock.calls.find(
      ([url, init]) =>
        String(url) === "/api/reporting/engagements/7/reports" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(JSON.parse(String((generateCall?.[1] as RequestInit).body))).toEqual({
      report_type: "pentest",
      selected_task_memo_ids: ["memo-43"],
      include_candidate_findings: false,
      force_regenerate: false,
    });
    expect(await screen.findByText("Report generation is ready.")).toBeTruthy();
    expect(
      await screen.findByRole("heading", {
        name: "Client Alpha Generated Report Preview",
      }),
    ).toBeTruthy();
    expect(screen.getByText("1 previous report.")).toBeTruthy();
    expect(screen.getByText(/Generated report body/)).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Open report Client Alpha Earlier Report version 1",
      }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "Client Alpha Earlier Report Preview",
      }),
    ).toBeTruthy();
    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/reports/report-old"),
    );
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Earlier Summary" })).toBeTruthy(),
    );
    expect(screen.getByText("Historical report body.")).toBeTruthy();
    expect(calledUrls().some((url) => url.startsWith("/api/reports"))).toBe(false);
    expect(
      invalidateSpy.mock.calls
        .map(([filters]) => JSON.stringify(filters))
        .every((filters) => filters.includes('"reporting"')),
    ).toBe(true);
  });
});
