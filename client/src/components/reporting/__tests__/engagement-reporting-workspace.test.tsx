// @vitest-environment jsdom
/* Tests for the top-level engagement reporting workspace container state. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EngagementReportingWorkspace } from "@/components/reporting/engagement-reporting-workspace";
import { Toaster } from "@/components/ui/toaster";
import type {
  EngagementReportHistoryItem,
  EngagementReportJobStatusResponse,
  EngagementReportReadResponse,
  ReportingInputState,
  ReportingInputTaskRow,
  TaskClosureMemoSummary,
} from "@/types/reporting";

const mocked = vi.hoisted(() => ({
  apiCall: vi.fn(),
  apiFetch: vi.fn(),
  tenantActions: ["report.write", "report.delete"],
}));

vi.mock("@/lib/api-config", () => ({
  apiCall: mocked.apiCall,
  apiFetch: mocked.apiFetch,
}));

vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: { actions: mocked.tenantActions },
  }),
}));

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
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

let reportingTasks: ReportingInputTaskRow[] = [];
let nextGenerateResponse: Response | null = null;
let nextActiveJob: Partial<EngagementReportJobStatusResponse> | null = null;
let nextJobStatus: Partial<EngagementReportJobStatusResponse> | null = null;
let currentReport: EngagementReportReadResponse | null = null;
let reportHistory: EngagementReportHistoryItem[] = [];
let prepareResponsesByTaskId = new Map<number, Response>();

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

function jobStatus(
  overrides: Partial<EngagementReportJobStatusResponse> = {},
): EngagementReportJobStatusResponse {
  return {
    id: "job-7",
    engagement_id: 7,
    report_id: null,
    report_type: "pentest",
    status: "generating",
    selected_task_memo_ids: ["memo-41"],
    include_candidate_findings: false,
    source_watermark: {},
    generation_phase: "sections",
    current_section_id: "executive_summary",
    completed_sections: ["scope"],
    total_sections: 3,
    attempt_count: 1,
    max_attempts: 3,
    last_error_code: null,
    error_message: null,
    next_attempt_at: null,
    last_error_at: null,
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    started_at: "2026-06-11T10:00:00Z",
    finished_at: null,
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
    version: 3,
    status: "ready",
    is_current: true,
    title: "Client Alpha Current Report",
    sections: [],
    markdown_snapshot: "## Executive Summary\n\nCurrent report body.",
    source_task_memo_ids: ["memo-41"],
    source_knowledge_refs: [
      {
        ref: "knowledge-1",
        task_id: 41,
        record_type: "observation",
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

function historyItem(
  overrides: Partial<EngagementReportHistoryItem> = {},
): EngagementReportHistoryItem {
  return {
    report_id: "report-current",
    engagement_id: 7,
    report_type: "pentest",
    version: 3,
    status: "ready",
    is_current: true,
    title: "Client Alpha Current Report",
    source_task_memo_ids: ["memo-41"],
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

function responseForUrl(url: string, init?: RequestInit): Response {
  if (url.startsWith("/api/engagements?")) {
    return jsonResponse({
      items: [engagementItem(7, "Client Alpha"), engagementItem(9, "Client Beta")],
      total: 2,
      limit: 100,
      offset: 0,
    });
  }
  if (url === "/api/llm/reporting-selection") {
    return jsonResponse({
      provider: "openai",
      model: "gpt-5.2",
      reasoning_effort: null,
      selection_status: {
        status: "selectable",
        selectable: true,
        runnable: true,
        reason: null,
      },
    });
  }
  if (url.includes("/inputs")) {
    const engagementId = Number(url.match(/engagements\/(\d+)/)?.[1] ?? 7);
    return jsonResponse({ engagement_id: engagementId, tasks: reportingTasks });
  }
  if (url.includes("/memo/prepare")) {
    const taskId = Number(url.match(/tasks\/(\d+)/)?.[1] ?? 0);
    const response = prepareResponsesByTaskId.get(taskId);
    if (response) {
      return response;
    }
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
        source_watermark: {},
        error_message: null,
        created_at: "2026-06-11T10:00:00Z",
        updated_at: "2026-06-11T10:00:00Z",
        generated_at: "2026-06-11T10:00:00Z",
        body: null,
      },
    });
  }
  if (
    url === "/api/reporting/engagements/7/reports" &&
    init?.method === "POST"
  ) {
    return (
      nextGenerateResponse ??
      jsonResponse({ job_id: "job-7", report_id: null, status: "queued" }, 202)
    );
  }
  if (url.includes("/jobs/active")) {
    return jsonResponse({
      job: nextActiveJob ? jobStatus(nextActiveJob) : null,
    });
  }
  if (url === "/api/reporting/jobs/job-7") {
    return jsonResponse(jobStatus(nextJobStatus ?? {}));
  }
  if (url === "/api/reporting/reports/report-current" && init?.method === "DELETE") {
    currentReport = null;
    return jsonResponse({
      report_id: "report-current",
      engagement_id: 7,
      report_type: "pentest",
      deleted_current: true,
      current_report_id: null,
      undo_until: "2026-06-11T10:06:00Z",
    });
  }
  if (
    url === "/api/reporting/reports/report-current/undo-delete" &&
    init?.method === "POST"
  ) {
    currentReport = reportResponse();
    return jsonResponse({
      report_id: "report-current",
      engagement_id: 7,
      report_type: "pentest",
      restored_current: true,
      current_report_id: "report-current",
    });
  }
  if (url === "/api/reporting/reports/report-7") {
    return jsonResponse({
      id: "report-7",
      schema_version: "engagement_report.v1",
      engagement_id: 7,
      report_type: "pentest",
      version: 2,
      status: "ready",
      is_current: true,
      title: "Client Alpha Report",
      sections: [],
      markdown_snapshot: null,
      source_task_memo_ids: ["memo-41"],
      source_knowledge_refs: [],
      source_evidence_refs: [],
      generation_metadata: null,
      error_message: null,
      created_at: "2026-06-11T10:00:00Z",
      updated_at: "2026-06-11T10:00:00Z",
      generated_at: "2026-06-11T10:00:00Z",
    });
  }
  if (url === "/api/reporting/reports/report-old") {
    return jsonResponse(
      reportResponse({
        id: "report-old",
        version: 1,
        is_current: false,
        title: "Client Alpha Historical Report",
        markdown_snapshot: "## Historical Summary\n\nEarlier report body.",
        generated_at: "2026-06-10T10:00:00Z",
      }),
    );
  }
  if (url.includes("/reports/current")) {
    const engagementId = Number(url.match(/engagements\/(\d+)/)?.[1] ?? 7);
    return jsonResponse({
      engagement_id: engagementId,
      report_type: "pentest",
      report: currentReport,
    });
  }
  if (url.includes("/reports/history")) {
    const engagementId = Number(url.match(/engagements\/(\d+)/)?.[1] ?? 7);
    return jsonResponse({
      engagement_id: engagementId,
      report_type: "pentest",
      reports: reportHistory,
    });
  }
  const engagementId = Number(url.match(/engagements\/(\d+)/)?.[1] ?? 7);
  return jsonResponse({
    engagement_id: engagementId,
    report_type: "pentest",
    reports: [],
  });
}

function renderWorkspace() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <EngagementReportingWorkspace />
      <Toaster />
    </QueryClientProvider>,
  );
  return { client, ...result };
}

function calledUrls(): string[] {
  return mocked.apiFetch.mock.calls.map(([url]) => String(url));
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((innerResolve) => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  mocked.tenantActions.splice(0, mocked.tenantActions.length, "report.write", "report.delete");
  window.history.replaceState({}, "", "/reports");
  reportingTasks = [];
  nextGenerateResponse = null;
  nextActiveJob = null;
  nextJobStatus = null;
  currentReport = null;
  reportHistory = [];
  prepareResponsesByTaskId = new Map();
  globalThis.ResizeObserver = TestResizeObserver;
  Element.prototype.scrollIntoView = vi.fn();
  mocked.apiCall.mockImplementation(async (url: string, init?: RequestInit) => {
    const response = responseForUrl(String(url), init);
    if (!response.ok) {
      throw new Error((await response.json())?.detail ?? "API request failed");
    }
    return response.json();
  });
  mocked.apiFetch.mockImplementation((url: string, init?: RequestInit) =>
    Promise.resolve(responseForUrl(String(url), init)),
  );
});

afterEach(() => {
  cleanup();
  mocked.apiCall.mockReset();
  mocked.apiFetch.mockReset();
});

describe("<EngagementReportingWorkspace />", () => {
  it("keeps viewer reporting controls read-only", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    mocked.tenantActions.splice(0, mocked.tenantActions.length);
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
    ];
    currentReport = reportResponse();

    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));

    expect(
      (screen.getByRole("button", { name: "Generate New Report" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Prepare Selected" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "Delete report" })).toBeNull();
    expect(
      screen.getAllByText("Your current tenant permissions allow report viewing only."),
    ).toHaveLength(2);
  });

  it("starts empty and does not call reporting mutation endpoints before engagement selection", async () => {
    renderWorkspace();

    expect(screen.getByRole("heading", { name: "Select an engagement" })).toBeTruthy();
    await waitFor(() => expect(calledUrls()).toContain("/api/engagements?limit=100"));
    expect(calledUrls().some((url) => url.includes("/api/reporting/"))).toBe(false);
    expect(calledUrls().some((url) => url.includes("/memo/prepare"))).toBe(false);
    expect(calledUrls().some((url) => url.endsWith("/reports"))).toBe(false);
  });

  it("enables inventory, current report, and history reads when an engagement is selected", async () => {
    renderWorkspace();

    fireEvent.click(await screen.findByRole("combobox", { name: "Engagement" }));
    fireEvent.click(await screen.findByText("Client Alpha"));

    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/engagements/7/inputs"),
    );

    expect(calledUrls()).toEqual(
      expect.arrayContaining([
        "/api/reporting/engagements/7/inputs",
        "/api/reporting/engagements/7/reports/current?report_type=pentest",
        "/api/reporting/engagements/7/reports/history?report_type=pentest",
        "/api/reporting/engagements/7/jobs/active?report_type=pentest",
      ]),
    );
    expect(calledUrls().some((url) => url.includes("/memo/prepare"))).toBe(false);
    expect(calledUrls().some((url) => url.endsWith("/reports"))).toBe(false);
  });

  it("renders current report preview from the reporting summary query only", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    currentReport = reportResponse();
    renderWorkspace();

    expect(await screen.findByText("Client Alpha Current Report Preview")).toBeTruthy();
    expect(screen.getByText("Version 3")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Executive Summary" })).toBeTruthy();
    expect(screen.getByText("Current report body.")).toBeTruthy();
    expect(calledUrls()).toEqual(
      expect.arrayContaining([
        "/api/reporting/engagements/7/reports/current?report_type=pentest",
        "/api/reporting/engagements/7/reports/history?report_type=pentest",
      ]),
    );
    expect(
      calledUrls().some((url) =>
        /transcripts|evidence|tool-output|workspace|\/api\/reports\b/.test(url),
      ),
    ).toBe(false);
  });

  it("opens a historical report through detail read without mutation", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    currentReport = reportResponse();
    reportHistory = [
      historyItem(),
      historyItem({
        report_id: "report-old",
        version: 1,
        is_current: false,
        title: "Client Alpha Historical Report",
        generated_at: "2026-06-10T10:00:00Z",
      }),
    ];
    renderWorkspace();

    expect(await screen.findByText("Client Alpha Current Report Preview")).toBeTruthy();
    expect(screen.getByText("1 previous report.")).toBeTruthy();
    expect(screen.queryByText("Current")).toBeNull();
    mocked.apiFetch.mockClear();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Open report Client Alpha Historical Report version 1",
      }),
    );

    expect(await screen.findByText("Client Alpha Historical Report Preview")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Historical Summary" })).toBeTruthy(),
    );
    expect(screen.getByText("Earlier report body.")).toBeTruthy();
    expect(calledUrls()).toContain("/api/reporting/reports/report-old");
    expect(
      mocked.apiFetch.mock.calls.some(
        ([url, init]) =>
          String(url).includes("/api/reporting/") &&
          (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(false);
    expect(calledUrls().some((url) => url.startsWith("/api/reports"))).toBe(false);
  });

  it("deletes a current report and offers undo", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    currentReport = reportResponse();
    renderWorkspace();

    expect(await screen.findByText("Client Alpha Current Report Preview")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Delete report/i }));

    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/reports/report-current"),
    );
    expect(await screen.findByText("Report deleted")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));

    await waitFor(() =>
      expect(calledUrls()).toContain(
        "/api/reporting/reports/report-current/undo-delete",
      ),
    );
  });

  it("uses a concrete-engagement selector without none or inline creation actions", async () => {
    renderWorkspace();

    fireEvent.click(await screen.findByRole("combobox", { name: "Engagement" }));

    expect(await screen.findByText("Client Alpha")).toBeTruthy();
    expect(screen.queryByText("None (auto-create from task name)")).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("Search engagements…"), {
      target: { value: "Unlisted Client" },
    });

    expect(await screen.findByText("No matching engagements.")).toBeTruthy();
    expect(screen.queryByText(/Create/)).toBeNull();
  });

  it("changes engagement through the selector and clears selected task input state", async () => {
    renderWorkspace();

    fireEvent.click(await screen.findByRole("combobox", { name: "Engagement" }));
    fireEvent.click(await screen.findByText("Client Alpha"));
    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/engagements/7/inputs"),
    );

    fireEvent.click(screen.getByRole("combobox", { name: "Engagement" }));
    fireEvent.click(await screen.findByText("Client Beta"));

    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/engagements/9/inputs"),
    );
    expect(screen.getByText("0 selected inputs")).toBeTruthy();
  });

  it("uses URL engagement preselection with the single engagement report type", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    renderWorkspace();

    await waitFor(() =>
      expect(calledUrls()).toContain(
        "/api/reporting/engagements/7/reports/history?report_type=pentest",
      ),
    );
    expect(screen.queryByRole("button", { name: "Engagement Report" })).toBeNull();
    expect(
      mocked.apiFetch.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/reports") &&
          (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(false);
    expect(calledUrls()).toContain("/api/reporting/engagements/7/inputs");
  });

  it("refreshes only selected engagement reporting inputs, current report, and history", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    const { client } = renderWorkspace();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    await waitFor(() =>
      expect(calledUrls()).toContain(
        "/api/reporting/engagements/7/reports/history?report_type=pentest",
      ),
    );
    const refreshButton = screen.getByRole("button", {
      name: "Refresh reporting workspace",
    }) as HTMLButtonElement;
    await waitFor(() => expect(refreshButton.disabled).toBe(false));
    mocked.apiFetch.mockClear();

    fireEvent.click(refreshButton);

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(4));
    expect(invalidateSpy.mock.calls.map(([filters]) => filters)).toEqual(
      expect.arrayContaining([
        { queryKey: ["reporting", "inputs", { engagement_id: 7 }] },
        {
          queryKey: [
            "reporting",
            "current-report",
            { engagement_id: 7, report_type: "pentest" },
          ],
        },
        {
          queryKey: [
            "reporting",
            "history",
            { engagement_id: 7, report_type: "pentest" },
          ],
        },
        {
          queryKey: [
            "reporting",
            "active-job",
            { engagement_id: 7, report_type: "pentest" },
          ],
        },
      ]),
    );
    expect(calledUrls()).toEqual(
      expect.arrayContaining([
        "/api/reporting/engagements/7/inputs",
        "/api/reporting/engagements/7/reports/current?report_type=pentest",
        "/api/reporting/engagements/7/reports/history?report_type=pentest",
        "/api/reporting/engagements/7/jobs/active?report_type=pentest",
      ]),
    );
    expect(calledUrls().every((url) => url.includes("/api/reporting/"))).toBe(true);
  });

  it("prepares selected eligible inputs without invalidating report summaries", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "not_prepared"),
      reportingRow(42, "stale"),
    ];
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 42" }));
    await waitFor(() => expect(screen.getByText("2 can be prepared.")).toBeTruthy());
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Prepare Selected" }));

    await waitFor(() => {
      expect(
        calledUrls().filter((url) => url.includes("/memo/prepare")).length,
      ).toBe(2);
    });
    const prepareCalls = mocked.apiFetch.mock.calls
      .filter(([url]) => String(url).includes("/memo/prepare"))
      .map(([url, init]) => ({
        url: String(url),
        body: JSON.parse(String((init as RequestInit).body)),
      }));
    expect(prepareCalls).toEqual(
      expect.arrayContaining([
        {
          url: "/api/reporting/tasks/41/memo/prepare",
          body: { regenerate: false },
        },
        {
          url: "/api/reporting/tasks/42/memo/prepare",
          body: { regenerate: true },
        },
      ]),
    );
    expect(calledUrls().some((url) => url.includes("/reports/current"))).toBe(false);
    expect(calledUrls().some((url) => url.includes("/reports/history"))).toBe(false);
    expect(calledUrls().some((url) => url.startsWith("/api/reports"))).toBe(false);
  });

  it("shows permission failures from selected input preparation without report invalidation", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "not_prepared"),
      reportingRow(42, "failed"),
    ];
    prepareResponsesByTaskId.set(
      42,
      jsonResponse({ detail: "Permission denied for reporting input" }, 403),
    );
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 42" }));
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Prepare Selected" }));

    expect(await screen.findByText("Some inputs need another preparation attempt.")).toBeTruthy();
    expect(calledUrls().filter((url) => url.includes("/memo/prepare"))).toHaveLength(2);
    expect(calledUrls().some((url) => url.includes("/reports/current"))).toBe(false);
    expect(calledUrls().some((url) => url.includes("/reports/history"))).toBe(false);
    expect(calledUrls().some((url) => url.startsWith("/api/reports"))).toBe(false);
  });

  it("shows in-flight prepare conflicts from single input preparation and refreshes inputs", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [reportingRow(41, "not_prepared")];
    prepareResponsesByTaskId.set(
      41,
      jsonResponse(
        { detail: "Task memo preparation is already in progress." },
        409,
      ),
    );
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Prepare Task 41" }));

    expect(await screen.findByText("Preparation blocked")).toBeTruthy();
    expect(
      await screen.findByText("Task memo preparation is already in progress."),
    ).toBeTruthy();
    await waitFor(() => {
      expect(calledUrls()).toContain("/api/reporting/engagements/7/inputs");
    });
  });

  it("generates from selected current memo IDs with candidate findings excluded by default and starts job polling", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
      reportingRow(42, "ready", { current_memo: memoSummary("memo-42") }),
      reportingRow(43, "stale", { current_memo: memoSummary("memo-43") }),
    ];
    renderWorkspace();

    expect(
      await screen.findByRole("switch", {
        name: "Include low-confidence candidate findings",
      }),
    ).toBeTruthy();
    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 42" }));
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Generate Report" }));

    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/jobs/job-7"),
    );
    const generateCall = mocked.apiFetch.mock.calls.find(
      ([url, init]) =>
        String(url) === "/api/reporting/engagements/7/reports" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(generateCall).toBeTruthy();
    expect(JSON.parse(String((generateCall?.[1] as RequestInit).body))).toEqual({
      report_type: "pentest",
      selected_task_memo_ids: ["memo-41", "memo-42"],
      include_candidate_findings: false,
      force_regenerate: false,
    });
    expect(await screen.findByText("Report generation is in progress.")).toBeTruthy();
    expect(screen.getByText("executive_summary")).toBeTruthy();
    expect(screen.getByText("1 of 3 completed")).toBeTruthy();
  });

  it("shows new report submission above an existing current report preview", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    currentReport = reportResponse();
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
    ];
    const generateDeferred = deferredResponse();
    mocked.apiFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (
        String(url) === "/api/reporting/engagements/7/reports" &&
        init?.method === "POST"
      ) {
        return generateDeferred.promise;
      }
      return Promise.resolve(responseForUrl(String(url), init));
    });
    renderWorkspace();

    expect(await screen.findByText("Client Alpha Current Report Preview")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));

    fireEvent.click(screen.getByRole("button", { name: "Generate New Report" }));

    expect(await screen.findByText("Submitting report generation request...")).toBeTruthy();
    expect(screen.getByText("Submitting")).toBeTruthy();
    expect(screen.getByText("Client Alpha Current Report Preview")).toBeTruthy();
    const generateCall = mocked.apiFetch.mock.calls.find(
      ([url, init]) =>
        String(url) === "/api/reporting/engagements/7/reports" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(JSON.parse(String((generateCall?.[1] as RequestInit).body))).toEqual({
      report_type: "pentest",
      selected_task_memo_ids: ["memo-41"],
      include_candidate_findings: false,
      force_regenerate: true,
    });

    generateDeferred.resolve(
      jsonResponse({ job_id: "job-7", report_id: null, status: "queued" }, 202),
    );
    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/jobs/job-7"),
    );
  });

  it("resumes polling an active report job after workspace mount", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    nextActiveJob = {
      id: "job-7",
      status: "generating",
      current_section_id: "findings",
      completed_sections: ["executive_summary", "scope"],
      total_sections: 5,
    };
    nextJobStatus = nextActiveJob;
    renderWorkspace();

    await waitFor(() =>
      expect(calledUrls()).toContain(
        "/api/reporting/engagements/7/jobs/active?report_type=pentest",
      ),
    );
    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/jobs/job-7"),
    );

    expect(await screen.findByText("Report generation is in progress.")).toBeTruthy();
    expect(screen.getByText("findings")).toBeTruthy();
    expect(screen.getByText("2 of 5 completed")).toBeTruthy();
  });

  it("includes candidate findings only after explicit low-confidence opt-in", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
    ];
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    fireEvent.click(
      screen.getByRole("switch", {
        name: "Include low-confidence candidate findings",
      }),
    );
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Generate Report" }));

    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/jobs/job-7"),
    );
    const generateCall = mocked.apiFetch.mock.calls.find(
      ([url, init]) =>
        String(url) === "/api/reporting/engagements/7/reports" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(JSON.parse(String((generateCall?.[1] as RequestInit).body))).toEqual(
      expect.objectContaining({ include_candidate_findings: true }),
    );
  });

  it("refreshes current report summaries when generation immediately returns a ready report", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
    ];
    nextGenerateResponse = jsonResponse(
      { job_id: null, report_id: "report-ready", status: "ready" },
      202,
    );
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Generate Report" }));

    await waitFor(() =>
      expect(calledUrls()).toContain(
        "/api/reporting/engagements/7/reports/current?report_type=pentest",
      ),
    );
    expect(calledUrls()).toContain(
      "/api/reporting/engagements/7/reports/history?report_type=pentest",
    );
    expect(calledUrls().some((url) => url.includes("/api/reports"))).toBe(false);
  });

  it("refreshes report summaries and fetches report detail when a job becomes ready", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
    ];
    nextGenerateResponse = jsonResponse(
      { job_id: "job-7", report_id: null, status: "queued" },
      202,
    );
    nextJobStatus = {
      status: "ready",
      report_id: "report-7",
      completed_sections: ["scope", "findings", "recommendations"],
      finished_at: "2026-06-11T10:01:00Z",
    };
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Generate Report" }));

    await waitFor(() =>
      expect(calledUrls()).toContain("/api/reporting/reports/report-7"),
    );
    expect(calledUrls()).toEqual(
      expect.arrayContaining([
        "/api/reporting/jobs/job-7",
        "/api/reporting/engagements/7/reports/current?report_type=pentest",
        "/api/reporting/engagements/7/reports/history?report_type=pentest",
      ]),
    );
    expect(screen.getByText("Report generation is ready.")).toBeTruthy();
  });

  it("shows failed job backend errors without clearing selected task inputs", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
    ];
    nextJobStatus = {
      status: "failed",
      error_message: "The selected report inputs are no longer valid.",
      finished_at: "2026-06-11T10:01:00Z",
    };
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Generate Report" }));

    expect(
      await screen.findByText("The selected report inputs are no longer valid."),
    ).toBeTruthy();
    expect(screen.getByText("1 selected input")).toBeTruthy();
    expect(
      screen
        .getByRole("checkbox", { name: "Select Task 41" })
        .getAttribute("aria-checked"),
    ).toBe("true");
    expect(calledUrls().filter((url) => url === "/api/reporting/jobs/job-7")).toHaveLength(1);
  });

  it("shows backend generation validation failures without clearing selection", async () => {
    window.history.replaceState({}, "", "/reports?engagement_id=7");
    reportingTasks = [
      reportingRow(41, "ready", { current_memo: memoSummary("memo-41") }),
    ];
    nextGenerateResponse = jsonResponse(
      { detail: "Selected memo is no longer current" },
      422,
    );
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Task 41")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task 41" }));
    mocked.apiFetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Generate Report" }));

    expect(await screen.findByText("Selected memo is no longer current")).toBeTruthy();
    expect(screen.getByText("1 selected input")).toBeTruthy();
    expect(
      screen
        .getByRole("checkbox", { name: "Select Task 41" })
        .getAttribute("aria-checked"),
    ).toBe("true");
  });
});
