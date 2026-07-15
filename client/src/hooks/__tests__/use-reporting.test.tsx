// @vitest-environment jsdom
/* Tests for reporting API keys, hooks, mutations, and input eligibility. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  canPrepareReportingInput,
  canSelectInputForGeneration,
  getGenerateDisabledReason,
  getPrepareDisabledReason,
  getPrepareSelection,
  getReportJobRefetchInterval,
  getSelectedReadyMemoIds,
  reportingKeys,
  useActiveEngagementReportJob,
  shouldRegeneratePreparedMemo,
  useCurrentEngagementReport,
  useEngagementReport,
  useEngagementReportHistory,
  useEngagementReportingInputs,
  useGenerateEngagementReport,
  usePrepareSelectedTaskMemos,
  usePrepareTaskMemo,
  useReportJobStatus,
  useTaskPanelReportingStatusProjection,
} from "@/hooks/use-reporting";
import type {
  EngagementReportJobStatusResponse,
  ReportingInputState,
  ReportingInputTaskRow,
  TaskClosureMemoSummary,
} from "@/types/reporting";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function wrapperWithClient(client: QueryClient) {
  return function ClientWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function memo(id: string): TaskClosureMemoSummary {
  return {
    id,
    version: 1,
    status: "ready",
    memo_mode: "supported",
    is_current: true,
    source_watermark: {
      last_chat_message_id: null,
      last_turn_sequence: null,
      latest_tool_execution_id: null,
      latest_evidence_created_at: null,
      latest_knowledge_observed_at: null,
    },
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
    current_memo: inputState === "ready" ? memo(`memo-${taskId}`) : null,
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

afterEach(() => {
  mocked.apiFetch.mockReset();
});

describe("reporting query keys", () => {
  it("include engagement, report type, job ID, and report ID dimensions", () => {
    expect(reportingKeys.inputs(12)).toEqual([
      "reporting",
      "inputs",
      { engagement_id: 12 },
    ]);
    expect(reportingKeys.currentReport(12, "pentest")).toEqual([
      "reporting",
      "current-report",
      { engagement_id: 12, report_type: "pentest" },
    ]);
    expect(reportingKeys.history(12, "vulnerability_assessment")).toEqual([
      "reporting",
      "history",
      { engagement_id: 12, report_type: "vulnerability_assessment" },
    ]);
    expect(reportingKeys.job("job-1")).toEqual([
      "reporting",
      "job",
      { job_id: "job-1" },
    ]);
    expect(reportingKeys.activeJob(12, "pentest")).toEqual([
      "reporting",
      "active-job",
      { engagement_id: 12, report_type: "pentest" },
    ]);
    expect(reportingKeys.report("report-1")).toEqual([
      "reporting",
      "report",
      { report_id: "report-1" },
    ]);
  });

  it("use product terminology in query key dimensions", () => {
    const releaseLabelToken = "wa" + "ve";
    const keyText = JSON.stringify([
      reportingKeys.inputs(12),
      reportingKeys.currentReport(12, "pentest"),
      reportingKeys.history(12, "vulnerability_assessment"),
      reportingKeys.job("job-1"),
      reportingKeys.activeJob(12, "pentest"),
      reportingKeys.report("report-1"),
    ]).toLowerCase();

    expect(keyText).not.toContain(releaseLabelToken);
  });
});

describe("reporting query hooks", () => {
  it("do not fetch when required identifiers are absent", () => {
    renderHook(() => useEngagementReportingInputs(null), { wrapper });
    renderHook(() => useCurrentEngagementReport(null, "pentest"), { wrapper });
    renderHook(() => useEngagementReportHistory(null, "pentest"), { wrapper });
    renderHook(() => useEngagementReport(null), { wrapper });
    renderHook(() => useReportJobStatus(null, { enabled: true }), { wrapper });
    renderHook(() => useActiveEngagementReportJob(null, "pentest"), { wrapper });
    renderHook(() => useTaskPanelReportingStatusProjection(null), { wrapper });

    expect(mocked.apiFetch).not.toHaveBeenCalled();
  });

  it("projects engagement inventory rows by task ID", async () => {
    mocked.apiFetch.mockResolvedValueOnce(
      jsonResponse({ engagement_id: 7, tasks: [row(101, "ready"), row(102, "stale")] }),
    );

    const { result } = renderHook(
      () => useTaskPanelReportingStatusProjection(7),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.inputByTaskId.size).toBe(2);
    });

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/reporting/engagements/7/inputs",
      expect.objectContaining({ method: "GET" }),
    );
    expect(result.current.engagementId).toBe(7);
    expect(result.current.hasInventory).toBe(true);
    expect(result.current.inputByTaskId.get(101)?.input_state).toBe("ready");
    expect(result.current.inputByTaskId.get(102)?.input_state).toBe("stale");
  });

  it("fetches only /api/reporting endpoints", async () => {
    mocked.apiFetch
      .mockResolvedValueOnce(jsonResponse({ engagement_id: 7, tasks: [] }))
      .mockResolvedValueOnce(
        jsonResponse({ engagement_id: 7, report_type: "pentest", report: null }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ engagement_id: 7, report_type: "pentest", reports: [] }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: "report-1", sections: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          id: "job-1",
          engagement_id: 7,
          report_id: null,
          report_type: "pentest",
          status: "generating",
          selected_task_memo_ids: [],
          include_candidate_findings: false,
          source_watermark: {},
          generation_phase: "sections",
          current_section_id: null,
          completed_sections: [],
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
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          job: {
            id: "job-2",
            engagement_id: 7,
            report_id: null,
            report_type: "pentest",
            status: "queued",
            selected_task_memo_ids: [],
            include_candidate_findings: false,
            source_watermark: {},
            generation_phase: "sections",
            current_section_id: null,
            completed_sections: [],
            total_sections: 0,
            attempt_count: 0,
            max_attempts: 3,
            last_error_code: null,
            error_message: null,
            next_attempt_at: null,
            last_error_at: null,
            created_at: "2026-06-11T10:00:00Z",
            updated_at: "2026-06-11T10:00:00Z",
            started_at: null,
            finished_at: null,
          },
        }),
      );

    renderHook(() => useEngagementReportingInputs(7), { wrapper });
    renderHook(() => useCurrentEngagementReport(7, "pentest"), { wrapper });
    renderHook(() => useEngagementReportHistory(7, "pentest"), { wrapper });
    renderHook(() => useEngagementReport("report-1"), { wrapper });
    renderHook(() => useReportJobStatus("job-1", { refetchInterval: false }), {
      wrapper,
    });
    renderHook(() => useActiveEngagementReportJob(7, "pentest"), { wrapper });

    await waitFor(() => {
      expect(mocked.apiFetch).toHaveBeenCalledTimes(6);
    });

    const paths = mocked.apiFetch.mock.calls.map((call) => call[0] as string);
    expect(paths).toEqual([
      "/api/reporting/engagements/7/inputs",
      "/api/reporting/engagements/7/reports/current?report_type=pentest",
      "/api/reporting/engagements/7/reports/history?report_type=pentest",
      "/api/reporting/reports/report-1",
      "/api/reporting/jobs/job-1",
      "/api/reporting/engagements/7/jobs/active?report_type=pentest",
    ]);
    expect(paths.every((path) => path.startsWith("/api/reporting/"))).toBe(true);
    expect(paths.some((path) => path.startsWith("/api/reports"))).toBe(false);
  });

  it("polls only queued or generating report jobs", () => {
    expect(getReportJobRefetchInterval(undefined, 2000)).toBe(2000);
    expect(
      getReportJobRefetchInterval(
        { status: "queued" } as EngagementReportJobStatusResponse,
        2000,
      ),
    ).toBe(2000);
    expect(
      getReportJobRefetchInterval(
        { status: "generating" } as EngagementReportJobStatusResponse,
        2000,
      ),
    ).toBe(2000);
    expect(
      getReportJobRefetchInterval(
        { status: "ready" } as EngagementReportJobStatusResponse,
        2000,
      ),
    ).toBe(false);
    expect(
      getReportJobRefetchInterval(
        { status: "failed" } as EngagementReportJobStatusResponse,
        2000,
      ),
    ).toBe(false);
    expect(
      getReportJobRefetchInterval(
        { status: "cancelled" } as EngagementReportJobStatusResponse,
        2000,
      ),
    ).toBe(false);
    expect(getReportJobRefetchInterval(undefined, false)).toBe(false);
  });
});

describe("reporting mutations", () => {
  it("prepares a task memo with backend request field names", async () => {
    mocked.apiFetch.mockResolvedValueOnce(
      jsonResponse({
        task_id: 42,
        memo: {
          id: "memo-42",
          schema_version: "task_closure_memo.v1",
          engagement_id: 7,
          task_id: 42,
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
      }),
    );

    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => usePrepareTaskMemo(), {
      wrapper: wrapperWithClient(client),
    });
    result.current.mutate({ task_id: 42, engagement_id: 7, regenerate: true });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/reporting/tasks/42/memo/prepare",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ regenerate: true }),
      }),
    );
    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledTimes(1);
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["reporting", "inputs", { engagement_id: 7 }],
    });
  });

  it("invalidates reporting inputs after a failed task memo prepare", async () => {
    mocked.apiFetch.mockResolvedValueOnce(
      jsonResponse(
        { detail: "Task memo preparation is already in progress." },
        409,
      ),
    );
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => usePrepareTaskMemo(), {
      wrapper: wrapperWithClient(client),
    });
    result.current.mutate({ task_id: 42, engagement_id: 7, regenerate: false });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });

    expect(result.current.error?.message).toBe(
      "Task memo preparation is already in progress.",
    );
    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["reporting", "inputs", { engagement_id: 7 }],
      });
    });
  });

  it("prepares selected task memos with controlled payloads and input-only invalidation", async () => {
    mocked.apiFetch
      .mockResolvedValueOnce(
        jsonResponse({
          task_id: 1,
          memo: {
            id: "memo-1",
            schema_version: "task_closure_memo.v1",
            engagement_id: 7,
            task_id: 1,
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
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          task_id: 2,
          memo: {
            id: "memo-2",
            schema_version: "task_closure_memo.v1",
            engagement_id: 7,
            task_id: 2,
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
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          task_id: 3,
          memo: {
            id: "memo-3",
            schema_version: "task_closure_memo.v1",
            engagement_id: 7,
            task_id: 3,
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
        }),
      );
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => usePrepareSelectedTaskMemos(), {
      wrapper: wrapperWithClient(client),
    });
    result.current.mutate({
      engagement_id: 7,
      rows: [row(1, "not_prepared"), row(2, "failed"), row(3, "stale")],
      concurrency: 2,
    });

    await waitFor(() => {
      expect(result.current.data?.completed).toBe(3);
    });

    const prepareCalls = mocked.apiFetch.mock.calls.map(([url, init]) => ({
      url: String(url),
      body: JSON.parse(String((init as RequestInit).body)),
    }));
    expect(prepareCalls).toEqual(
      expect.arrayContaining([
        {
          url: "/api/reporting/tasks/1/memo/prepare",
          body: { regenerate: false },
        },
        {
          url: "/api/reporting/tasks/2/memo/prepare",
          body: { regenerate: true },
        },
        {
          url: "/api/reporting/tasks/3/memo/prepare",
          body: { regenerate: true },
        },
      ]),
    );
    expect(invalidateSpy).toHaveBeenCalledTimes(4);
    expect(invalidateSpy.mock.calls.map(([filters]) => filters)).toEqual(
      Array.from({ length: 4 }, () => ({
        queryKey: ["reporting", "inputs", { engagement_id: 7 }],
      })),
    );
  });

  it("keeps selected preparation failures available for retry display", async () => {
    mocked.apiFetch
      .mockResolvedValueOnce(
        jsonResponse({
          task_id: 1,
          memo: {
            id: "memo-1",
            schema_version: "task_closure_memo.v1",
            engagement_id: 7,
            task_id: 1,
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
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ detail: "Permission denied" }, 403));

    const { result } = renderHook(() => usePrepareSelectedTaskMemos(), {
      wrapper,
    });
    result.current.mutate({
      engagement_id: 7,
      rows: [row(1, "not_prepared"), row(2, "failed")],
    });

    await waitFor(() => {
      expect(result.current.data?.completed).toBe(2);
    });

    expect(result.current.data?.failed).toBe(1);
    expect(result.current.data?.results).toContainEqual(
      expect.objectContaining({
        task_id: 2,
        ok: false,
        error_message: "Permission denied",
      }),
    );
  });

  it("generates an engagement report with selected memo IDs and candidate findings excluded by default", async () => {
    mocked.apiFetch.mockResolvedValueOnce(
      jsonResponse({ job_id: "job-7", report_id: null, status: "queued" }, 202),
    );

    const { result } = renderHook(() => useGenerateEngagementReport(), {
      wrapper,
    });
    result.current.mutate({
      engagement_id: 7,
      report_type: "pentest",
      selected_task_memo_ids: ["memo-1", "memo-2"],
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/reporting/engagements/7/reports",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          report_type: "pentest",
          selected_task_memo_ids: ["memo-1", "memo-2"],
          include_candidate_findings: false,
          force_regenerate: false,
        }),
      }),
    );
  });

  it("uses backend error detail for mutation failures", async () => {
    mocked.apiFetch.mockResolvedValueOnce(
      jsonResponse({ detail: "Select at least one task closure memo" }, 422),
    );

    const { result } = renderHook(() => useGenerateEngagementReport(), {
      wrapper,
    });
    result.current.mutate({
      engagement_id: 7,
      report_type: "pentest",
      selected_task_memo_ids: [],
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toBe(
      "Select at least one task closure memo",
    );
  });
});

describe("reporting input eligibility", () => {
  it("selects only current ready memo ids for generation", () => {
    const rows = [
      row(1, "ready"),
      row(2, "ready", { current_memo: null }),
      row(3, "stale", { current_memo: memo("memo-3") }),
      row(4, "failed"),
      row(5, "preparing"),
    ];

    expect(getSelectedReadyMemoIds(rows, [1, 2, 3, 4, 5])).toEqual(["memo-1"]);
    expect(canSelectInputForGeneration(rows[0])).toBe(true);
    expect(canSelectInputForGeneration(rows[1])).toBe(false);
    expect(canSelectInputForGeneration(rows[2])).toBe(false);
    expect(canSelectInputForGeneration(rows[4])).toBe(false);
  });

  it("selects only preparable missing, failed, and stale rows for preparation", () => {
    const rows = [
      row(1, "not_prepared"),
      row(2, "failed"),
      row(3, "stale"),
      row(4, "preparing", { is_preparable: true }),
      row(5, "ready", { is_preparable: true }),
      row(6, "failed", { is_preparable: false }),
    ];

    expect(getPrepareSelection(rows, [1, 2, 3, 4, 5, 6]).map((item) => item.task_id)).toEqual([
      1,
      2,
      3,
    ]);
    expect(canPrepareReportingInput(rows[0])).toBe(true);
    expect(canPrepareReportingInput(rows[3])).toBe(false);
    expect(canPrepareReportingInput(rows[5])).toBe(false);
    expect(shouldRegeneratePreparedMemo(rows[0])).toBe(false);
    expect(shouldRegeneratePreparedMemo(rows[1])).toBe(true);
    expect(shouldRegeneratePreparedMemo(rows[2])).toBe(true);
  });

  it("allows ready selected rows even when local task status is not enough to decide", () => {
    const rows = [
      row(1, "ready", {
        task_status: "running",
        candidate_findings_require_explicit_inclusion: true,
        counts: {
          evidence: 1,
          canonical_findings: 1,
          candidate_findings: 1,
        },
      }),
    ];

    expect(
      getGenerateDisabledReason({
        rows,
        selectedTaskIds: [1],
        engagementId: 7,
        reportType: "pentest",
      }),
    ).toBeNull();
    expect(
      getGenerateDisabledReason({
        rows,
        selectedTaskIds: [1],
        engagementId: 7,
        reportType: "pentest",
      }),
    ).toBeNull();
    expect(
      getGenerateDisabledReason({
        rows,
        selectedTaskIds: [],
        engagementId: 7,
        reportType: "pentest",
      }),
    ).toBe("Select ready inputs to generate a report.");
  });

  it("reports detailed disabled reasons for prepare and generate actions", () => {
    const rows = [
      row(1, "ready", { current_memo: null }),
      row(2, "stale", { current_memo: memo("memo-2") }),
      row(3, "failed"),
      row(4, "preparing"),
      row(5, "not_prepared"),
    ];

    expect(
      getPrepareDisabledReason({
        rows,
        selectedTaskIds: [5],
        engagementId: null,
      }),
    ).toBe("Select an engagement to prepare inputs.");
    expect(
      getPrepareDisabledReason({
        rows,
        selectedTaskIds: [4],
        engagementId: 7,
      }),
    ).toBe("Selected input is already preparing.");
    expect(
      getPrepareDisabledReason({
        rows,
        selectedTaskIds: [1],
        engagementId: 7,
      }),
    ).toBe("No selected inputs need preparation.");

    expect(
      getGenerateDisabledReason({
        rows,
        selectedTaskIds: [1],
        engagementId: 7,
        reportType: "pentest",
      }),
    ).toBe("Selected ready input is missing its current memo.");
    expect(
      getGenerateDisabledReason({
        rows,
        selectedTaskIds: [2],
        engagementId: 7,
        reportType: "pentest",
      }),
    ).toBe("Selected input is stale. Regenerate it before generating a report.");
    expect(
      getGenerateDisabledReason({
        rows,
        selectedTaskIds: [3],
        engagementId: 7,
        reportType: "pentest",
      }),
    ).toBe("Selected input failed preparation. Retry preparation before generating a report.");
    expect(
      getGenerateDisabledReason({
        rows,
        selectedTaskIds: [4],
        engagementId: 7,
        reportType: "pentest",
      }),
    ).toBe("Selected input is still preparing.");
  });
});
