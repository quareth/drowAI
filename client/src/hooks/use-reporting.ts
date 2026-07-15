/* Central React Query boundary for the engagement reporting API. */

import { useMemo, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type QueryFunctionContext,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiFetch } from "@/lib/api-config";
import type {
  CurrentEngagementReportResponse,
  EngagementReportActiveJobResponse,
  EngagementReportDeleteResponse,
  EngagementReportGenerationRequest,
  EngagementReportGenerationResponse,
  EngagementReportHistoryResponse,
  EngagementReportJobStatusResponse,
  EngagementReportReadResponse,
  EngagementReportUndoDeleteResponse,
  EngagementReportingInputsResponse,
  ReportLibraryResponse,
  ReportType,
  ReportingInputTaskRow,
  TaskClosureMemoPrepareRequest,
  TaskClosureMemoPrepareResponse,
  UUIDString,
} from "@/types/reporting";

type ReportingId = number | null | undefined;
type ReportingStringId = string | null | undefined;

export const reportingKeys = {
  inputs: (engagementId: ReportingId) =>
    ["reporting", "inputs", { engagement_id: engagementId ?? null }] as const,
  currentReport: (engagementId: ReportingId, reportType: ReportType) =>
    [
      "reporting",
      "current-report",
      { engagement_id: engagementId ?? null, report_type: reportType },
    ] as const,
  history: (engagementId: ReportingId, reportType: ReportType) =>
    [
      "reporting",
      "history",
      { engagement_id: engagementId ?? null, report_type: reportType },
    ] as const,
  report: (reportId: ReportingStringId) =>
    ["reporting", "report", { report_id: reportId ?? null }] as const,
  job: (jobId: ReportingStringId) =>
    ["reporting", "job", { job_id: jobId ?? null }] as const,
  activeJob: (engagementId: ReportingId, reportType: ReportType) =>
    [
      "reporting",
      "active-job",
      { engagement_id: engagementId ?? null, report_type: reportType },
    ] as const,
  library: (filters?: ReportLibraryFilters) =>
    ["reporting", "library", filters ?? {}] as const,
};

const ACTIVE_JOB_STATUSES = new Set(["queued", "generating"]);

const PREPARABLE_INPUT_STATES = new Set<ReportingInputTaskRow["input_state"]>([
  "not_prepared",
  "failed",
  "stale",
]);

const DEFAULT_PREPARE_SELECTED_CONCURRENCY = 2;
const MAX_PREPARE_SELECTED_CONCURRENCY = 3;

export interface ReportJobStatusOptions {
  enabled?: boolean;
  refetchInterval?: number | false;
}

export interface PrepareTaskMemoVariables {
  task_id: number;
  engagement_id?: number | null;
  regenerate?: boolean;
}

export interface PrepareSelectedTaskMemosVariables {
  engagement_id: number;
  rows: ReportingInputTaskRow[];
  concurrency?: number;
}

export interface PrepareSelectedTaskMemoResult {
  task_id: number;
  task_name: string;
  regenerate: boolean;
  ok: boolean;
  error_message: string | null;
}

export interface PrepareSelectedTaskMemosResult {
  total: number;
  completed: number;
  failed: number;
  succeeded: number;
  results: PrepareSelectedTaskMemoResult[];
}

export interface PrepareSelectedTaskMemosProgress {
  total: number;
  completed: number;
  failed: number;
  inFlightTaskIds: number[];
}

export interface GenerateEngagementReportVariables {
  engagement_id: number;
  report_type: ReportType;
  selected_task_memo_ids: UUIDString[];
  include_candidate_findings?: boolean;
  force_regenerate?: boolean;
}

export interface DeleteEngagementReportVariables {
  report_id: UUIDString;
  engagement_id?: number | null;
  report_type?: ReportType;
}

export interface UndoDeleteEngagementReportVariables {
  report_id: UUIDString;
  engagement_id?: number | null;
  report_type?: ReportType;
}

export interface ReportLibraryFilters {
  report_type?: ReportType;
  engagement_id?: number;
  query?: string;
  limit?: number;
  offset?: number;
}

export interface GenerateDisabledState {
  rows: ReportingInputTaskRow[];
  selectedTaskIds: Iterable<number>;
  includeCandidateFindings?: boolean;
  isGenerating?: boolean;
  engagementId?: number | null;
  reportType?: ReportType | null;
}

export interface PrepareDisabledState {
  rows: ReportingInputTaskRow[];
  selectedTaskIds: Iterable<number>;
  isPreparingSelected?: boolean;
  engagementId?: number | null;
}

export interface TaskPanelReportingStatusProjection {
  engagementId: number | null;
  inputByTaskId: Map<number, ReportingInputTaskRow>;
  hasInventory: boolean;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

async function reportingErrorMessage(response: Response): Promise<string> {
  const fallback = `${response.status}: ${response.statusText || "Reporting request failed"}`;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const body = (await response.json().catch(() => null)) as
      | { detail?: unknown; message?: unknown }
      | null;
    const detail = body?.detail ?? body?.message;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
  }
  const text = await response.text().catch(() => "");
  return text.trim() || fallback;
}

async function fetchReportingJson<T>(
  endpoint: string,
  signal?: AbortSignal,
  init?: RequestInit,
): Promise<T> {
  const response = await apiFetch(endpoint, {
    method: init?.method ?? "GET",
    ...init,
    signal,
  });
  if (!response.ok) {
    throw new Error(await reportingErrorMessage(response));
  }
  return response.json() as Promise<T>;
}

function reportTypeQuery(reportType: ReportType): string {
  const params = new URLSearchParams({ report_type: reportType });
  return params.toString();
}

function reportLibraryQuery(filters: ReportLibraryFilters = {}): string {
  const params = new URLSearchParams();
  if (filters.report_type) params.set("report_type", filters.report_type);
  if (typeof filters.engagement_id === "number") {
    params.set("engagement_id", String(filters.engagement_id));
  }
  const query = filters.query?.trim();
  if (query) params.set("query", query);
  if (typeof filters.limit === "number") params.set("limit", String(filters.limit));
  if (typeof filters.offset === "number") params.set("offset", String(filters.offset));
  const value = params.toString();
  return value ? `?${value}` : "";
}

function normalizeNumericId(id: ReportingId): number | null {
  return typeof id === "number" && Number.isFinite(id) ? id : null;
}

function normalizeStringId(id: ReportingStringId): string | null {
  const value = id?.trim();
  return value ? value : null;
}

function prepareSelectedConcurrency(value: number | undefined): number {
  if (!Number.isFinite(value)) {
    return DEFAULT_PREPARE_SELECTED_CONCURRENCY;
  }
  return Math.min(
    MAX_PREPARE_SELECTED_CONCURRENCY,
    Math.max(1, Math.floor(value ?? DEFAULT_PREPARE_SELECTED_CONCURRENCY)),
  );
}

export function getReportJobRefetchInterval(
  job: EngagementReportJobStatusResponse | undefined,
  interval: number | false | undefined,
): number | false {
  if (job && !ACTIVE_JOB_STATUSES.has(job.status)) {
    return false;
  }
  return interval ?? false;
}

export async function fetchEngagementReportingInputs(
  engagementId: number,
  signal?: AbortSignal,
): Promise<EngagementReportingInputsResponse> {
  return fetchReportingJson<EngagementReportingInputsResponse>(
    `/api/reporting/engagements/${engagementId}/inputs`,
    signal,
  );
}

export async function fetchCurrentEngagementReport(
  engagementId: number,
  reportType: ReportType,
  signal?: AbortSignal,
): Promise<CurrentEngagementReportResponse> {
  return fetchReportingJson<CurrentEngagementReportResponse>(
    `/api/reporting/engagements/${engagementId}/reports/current?${reportTypeQuery(reportType)}`,
    signal,
  );
}

export async function fetchEngagementReportHistory(
  engagementId: number,
  reportType: ReportType,
  signal?: AbortSignal,
): Promise<EngagementReportHistoryResponse> {
  return fetchReportingJson<EngagementReportHistoryResponse>(
    `/api/reporting/engagements/${engagementId}/reports/history?${reportTypeQuery(reportType)}`,
    signal,
  );
}

export async function fetchEngagementReport(
  reportId: string,
  signal?: AbortSignal,
): Promise<EngagementReportReadResponse> {
  return fetchReportingJson<EngagementReportReadResponse>(
    `/api/reporting/reports/${reportId}`,
    signal,
  );
}

export async function fetchReportLibrary(
  filters: ReportLibraryFilters = {},
  signal?: AbortSignal,
): Promise<ReportLibraryResponse> {
  return fetchReportingJson<ReportLibraryResponse>(
    `/api/reporting/reports${reportLibraryQuery(filters)}`,
    signal,
  );
}

export async function deleteEngagementReport(
  reportId: string,
): Promise<EngagementReportDeleteResponse> {
  return fetchReportingJson<EngagementReportDeleteResponse>(
    `/api/reporting/reports/${reportId}`,
    undefined,
    { method: "DELETE" },
  );
}

export async function undoDeleteEngagementReport(
  reportId: string,
): Promise<EngagementReportUndoDeleteResponse> {
  return fetchReportingJson<EngagementReportUndoDeleteResponse>(
    `/api/reporting/reports/${reportId}/undo-delete`,
    undefined,
    { method: "POST" },
  );
}

export async function fetchReportJobStatus(
  jobId: string,
  signal?: AbortSignal,
): Promise<EngagementReportJobStatusResponse> {
  return fetchReportingJson<EngagementReportJobStatusResponse>(
    `/api/reporting/jobs/${jobId}`,
    signal,
  );
}

export async function fetchActiveEngagementReportJob(
  engagementId: number,
  reportType: ReportType,
  signal?: AbortSignal,
): Promise<EngagementReportActiveJobResponse> {
  return fetchReportingJson<EngagementReportActiveJobResponse>(
    `/api/reporting/engagements/${engagementId}/jobs/active?${reportTypeQuery(reportType)}`,
    signal,
  );
}

export async function prepareTaskMemoRequest({
  task_id,
  regenerate = false,
}: PrepareTaskMemoVariables): Promise<TaskClosureMemoPrepareResponse> {
  return fetchReportingJson<TaskClosureMemoPrepareResponse>(
    `/api/reporting/tasks/${task_id}/memo/prepare`,
    undefined,
    {
      method: "POST",
      body: JSON.stringify({
        regenerate,
      } satisfies TaskClosureMemoPrepareRequest),
    },
  );
}

export function useEngagementReportingInputs(
  engagementId: ReportingId,
): UseQueryResult<EngagementReportingInputsResponse> {
  const id = normalizeNumericId(engagementId);
  return useQuery({
    queryKey: reportingKeys.inputs(id),
    enabled: id !== null,
    queryFn: ({ signal }: QueryFunctionContext) => {
      if (id === null) throw new Error("Engagement is required");
      return fetchEngagementReportingInputs(id, signal);
    },
  });
}

export function useTaskPanelReportingStatusProjection(
  engagementId: ReportingId,
): TaskPanelReportingStatusProjection {
  const id = normalizeNumericId(engagementId);
  const inventory = useEngagementReportingInputs(id);
  const inputByTaskId = useMemo(() => {
    if (id === null || inventory.data?.engagement_id !== id) {
      return new Map<number, ReportingInputTaskRow>();
    }
    return new Map(inventory.data.tasks.map((row) => [row.task_id, row]));
  }, [id, inventory.data]);

  return {
    engagementId: id,
    inputByTaskId,
    hasInventory: inputByTaskId.size > 0,
    isLoading: inventory.isLoading,
    isError: inventory.isError,
    error: inventory.error ?? null,
  };
}

export function useCurrentEngagementReport(
  engagementId: ReportingId,
  reportType: ReportType,
): UseQueryResult<CurrentEngagementReportResponse> {
  const id = normalizeNumericId(engagementId);
  return useQuery({
    queryKey: reportingKeys.currentReport(id, reportType),
    enabled: id !== null,
    queryFn: ({ signal }: QueryFunctionContext) => {
      if (id === null) throw new Error("Engagement is required");
      return fetchCurrentEngagementReport(id, reportType, signal);
    },
  });
}

export function useEngagementReportHistory(
  engagementId: ReportingId,
  reportType: ReportType,
): UseQueryResult<EngagementReportHistoryResponse> {
  const id = normalizeNumericId(engagementId);
  return useQuery({
    queryKey: reportingKeys.history(id, reportType),
    enabled: id !== null,
    queryFn: ({ signal }: QueryFunctionContext) => {
      if (id === null) throw new Error("Engagement is required");
      return fetchEngagementReportHistory(id, reportType, signal);
    },
  });
}

export function useEngagementReport(
  reportId: ReportingStringId,
): UseQueryResult<EngagementReportReadResponse> {
  const id = normalizeStringId(reportId);
  return useQuery({
    queryKey: reportingKeys.report(id),
    enabled: id !== null,
    queryFn: ({ signal }: QueryFunctionContext) => {
      if (id === null) throw new Error("Report is required");
      return fetchEngagementReport(id, signal);
    },
  });
}

export function useReportLibrary(
  filters: ReportLibraryFilters = {},
): UseQueryResult<ReportLibraryResponse> {
  return useQuery({
    queryKey: reportingKeys.library(filters),
    queryFn: ({ signal }: QueryFunctionContext) => fetchReportLibrary(filters, signal),
  });
}

export function useReportJobStatus(
  jobId: ReportingStringId,
  options: ReportJobStatusOptions = {},
): UseQueryResult<EngagementReportJobStatusResponse> {
  const id = normalizeStringId(jobId);
  return useQuery({
    queryKey: reportingKeys.job(id),
    enabled: id !== null && (options.enabled ?? true),
    queryFn: ({ signal }: QueryFunctionContext) => {
      if (id === null) throw new Error("Report job is required");
      return fetchReportJobStatus(id, signal);
    },
    refetchInterval: (query) =>
      getReportJobRefetchInterval(
        query.state.data as EngagementReportJobStatusResponse | undefined,
        options.refetchInterval,
      ),
  });
}

export function useActiveEngagementReportJob(
  engagementId: ReportingId,
  reportType: ReportType,
  options: ReportJobStatusOptions = {},
): UseQueryResult<EngagementReportActiveJobResponse> {
  const id = normalizeNumericId(engagementId);
  return useQuery({
    queryKey: reportingKeys.activeJob(id, reportType),
    enabled: id !== null && (options.enabled ?? true),
    queryFn: ({ signal }: QueryFunctionContext) => {
      if (id === null) throw new Error("Engagement is required");
      return fetchActiveEngagementReportJob(id, reportType, signal);
    },
    refetchInterval: (query) =>
      getReportJobRefetchInterval(
        (query.state.data as EngagementReportActiveJobResponse | undefined)?.job ??
          undefined,
        options.refetchInterval,
      ),
  });
}

export async function invalidateReportingInputs(
  queryClient: QueryClient,
  engagementId: ReportingId,
): Promise<void> {
  await queryClient.invalidateQueries({
    queryKey: reportingKeys.inputs(normalizeNumericId(engagementId)),
  });
}

export async function invalidateReportSummaries(
  queryClient: QueryClient,
  engagementId: ReportingId,
  reportType: ReportType,
): Promise<void> {
  const id = normalizeNumericId(engagementId);
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: reportingKeys.currentReport(id, reportType) }),
    queryClient.invalidateQueries({ queryKey: reportingKeys.history(id, reportType) }),
  ]);
}

export function usePrepareTaskMemo() {
  const queryClient = useQueryClient();
  return useMutation<TaskClosureMemoPrepareResponse, Error, PrepareTaskMemoVariables>({
    mutationFn: prepareTaskMemoRequest,
    onSettled: async (_data, _error, variables) => {
      if (variables.engagement_id !== undefined) {
        await invalidateReportingInputs(queryClient, variables.engagement_id);
      }
    },
  });
}

export function usePrepareSelectedTaskMemos() {
  const queryClient = useQueryClient();
  const [progress, setProgress] =
    useState<PrepareSelectedTaskMemosProgress | null>(null);

  const mutation = useMutation<
    PrepareSelectedTaskMemosResult,
    Error,
    PrepareSelectedTaskMemosVariables
  >({
    mutationFn: async ({ engagement_id, rows, concurrency }) => {
      const selectedRows = rows.filter(canPrepareReportingInput);
      const total = selectedRows.length;
      const results: Array<PrepareSelectedTaskMemoResult | undefined> = new Array(
        total,
      );
      const inFlightTaskIds = new Set<number>();
      let nextIndex = 0;
      let completed = 0;
      let failed = 0;

      const publishProgress = () => {
        setProgress({
          total,
          completed,
          failed,
          inFlightTaskIds: [...inFlightTaskIds],
        });
      };

      publishProgress();
      if (total === 0) {
        return {
          total: 0,
          completed: 0,
          failed: 0,
          succeeded: 0,
          results: [],
        };
      }

      await invalidateReportingInputs(queryClient, engagement_id);

      const runNext = async (): Promise<void> => {
        const index = nextIndex;
        nextIndex += 1;
        const row = selectedRows[index];
        if (!row) {
          return;
        }

        const regenerate = shouldRegeneratePreparedMemo(row);
        inFlightTaskIds.add(row.task_id);
        publishProgress();

        try {
          await prepareTaskMemoRequest({
            task_id: row.task_id,
            regenerate,
          });
          results[index] = {
            task_id: row.task_id,
            task_name: row.task_name,
            regenerate,
            ok: true,
            error_message: null,
          };
        } catch (error) {
          failed += 1;
          results[index] = {
            task_id: row.task_id,
            task_name: row.task_name,
            regenerate,
            ok: false,
            error_message:
              error instanceof Error && error.message.trim()
                ? error.message
                : "Memo preparation failed.",
          };
        } finally {
          completed += 1;
          inFlightTaskIds.delete(row.task_id);
          publishProgress();
          await invalidateReportingInputs(queryClient, engagement_id);
        }

        await runNext();
      };

      const workerCount = Math.min(
        prepareSelectedConcurrency(concurrency),
        total,
      );
      await Promise.all(Array.from({ length: workerCount }, () => runNext()));

      return {
        total,
        completed,
        failed,
        succeeded: total - failed,
        results: results.filter(
          (result): result is PrepareSelectedTaskMemoResult => Boolean(result),
        ),
      };
    },
  });

  const preparingTaskIds = useMemo(
    () => new Set(progress?.inFlightTaskIds ?? []),
    [progress],
  );

  return {
    ...mutation,
    progress,
    preparingTaskIds,
  };
}

export function useGenerateEngagementReport() {
  const queryClient = useQueryClient();
  return useMutation<
    EngagementReportGenerationResponse,
    Error,
    GenerateEngagementReportVariables
  >({
    mutationFn: ({
      engagement_id,
      report_type,
      selected_task_memo_ids,
      include_candidate_findings = false,
      force_regenerate = false,
    }) =>
      fetchReportingJson<EngagementReportGenerationResponse>(
        `/api/reporting/engagements/${engagement_id}/reports`,
        undefined,
        {
          method: "POST",
          body: JSON.stringify({
            report_type,
            selected_task_memo_ids,
            include_candidate_findings,
            force_regenerate,
          } satisfies EngagementReportGenerationRequest),
        },
      ),
    onSuccess: async (_data, variables) => {
      await invalidateReportSummaries(
        queryClient,
        variables.engagement_id,
        variables.report_type,
      );
      await queryClient.invalidateQueries({ queryKey: ["reporting", "library"] });
    },
  });
}

export function useDeleteEngagementReport() {
  const queryClient = useQueryClient();
  return useMutation<
    EngagementReportDeleteResponse,
    Error,
    DeleteEngagementReportVariables
  >({
    mutationFn: ({ report_id }) => deleteEngagementReport(report_id),
    onSuccess: async (data, variables) => {
      queryClient.removeQueries({ queryKey: reportingKeys.report(data.report_id) });
      await invalidateReportSummaries(
        queryClient,
        variables.engagement_id ?? data.engagement_id,
        variables.report_type ?? data.report_type,
      );
      if (data.current_report_id) {
        await queryClient.invalidateQueries({
          queryKey: reportingKeys.report(data.current_report_id),
        });
      }
      await queryClient.invalidateQueries({ queryKey: ["reporting", "library"] });
    },
  });
}

export function useUndoDeleteEngagementReport() {
  const queryClient = useQueryClient();
  return useMutation<
    EngagementReportUndoDeleteResponse,
    Error,
    UndoDeleteEngagementReportVariables
  >({
    mutationFn: ({ report_id }) => undoDeleteEngagementReport(report_id),
    onSuccess: async (data, variables) => {
      await invalidateReportSummaries(
        queryClient,
        variables.engagement_id ?? data.engagement_id,
        variables.report_type ?? data.report_type,
      );
      await queryClient.invalidateQueries({
        queryKey: reportingKeys.report(data.report_id),
      });
      if (data.current_report_id) {
        await queryClient.invalidateQueries({
          queryKey: reportingKeys.report(data.current_report_id),
        });
      }
      await queryClient.invalidateQueries({ queryKey: ["reporting", "library"] });
    },
  });
}

export function canPrepareReportingInput(row: ReportingInputTaskRow): boolean {
  return row.is_preparable && PREPARABLE_INPUT_STATES.has(row.input_state);
}

export function shouldRegeneratePreparedMemo(row: ReportingInputTaskRow): boolean {
  return row.input_state === "failed" || row.input_state === "stale";
}

export function canSelectInputForGeneration(row: ReportingInputTaskRow): boolean {
  return row.input_state === "ready" && Boolean(row.current_memo?.id);
}

export function getSelectedReadyMemoIds(
  rows: ReportingInputTaskRow[],
  selectedTaskIds: Iterable<number>,
): string[] {
  const selected = new Set(selectedTaskIds);
  return rows
    .filter((row) => selected.has(row.task_id) && canSelectInputForGeneration(row))
    .map((row) => row.current_memo?.id)
    .filter((id): id is string => Boolean(id));
}

export function getPrepareSelection(
  rows: ReportingInputTaskRow[],
  selectedTaskIds: Iterable<number>,
): ReportingInputTaskRow[] {
  const selected = new Set(selectedTaskIds);
  return rows.filter(
    (row) => selected.has(row.task_id) && canPrepareReportingInput(row),
  );
}

export function getPrepareDisabledReason(state: PrepareDisabledState): string | null {
  if (!state.engagementId) {
    return "Select an engagement to prepare inputs.";
  }
  if (state.isPreparingSelected) {
    return "Selected input preparation is already running.";
  }

  const selected = new Set(state.selectedTaskIds);
  if (selected.size === 0) {
    return "Select inputs that need preparation.";
  }

  const selectedRows = state.rows.filter((row) => selected.has(row.task_id));
  if (selectedRows.length === 0) {
    return "Selected inputs are no longer available.";
  }
  if (selectedRows.some((row) => row.input_state === "preparing")) {
    return "Selected input is already preparing.";
  }
  if (getPrepareSelection(state.rows, selected).length === 0) {
    return selectedRows.every((row) => row.input_state === "ready")
      ? "No selected inputs need preparation."
      : "No selected inputs can be prepared.";
  }

  return null;
}

export function getGenerateDisabledReason(state: GenerateDisabledState): string | null {
  if (!state.engagementId) {
    return "Select an engagement to generate a report.";
  }
  if (!state.reportType) {
    return "Select a report type.";
  }
  if (state.isGenerating) {
    return "Report generation is already running.";
  }

  const selected = new Set(state.selectedTaskIds);
  if (selected.size === 0) {
    return "Select ready inputs to generate a report.";
  }

  const selectedRows = state.rows.filter((row) => selected.has(row.task_id));
  if (selectedRows.length === 0) {
    return "Selected inputs are no longer available.";
  }
  if (selectedRows.some((row) => row.input_state === "preparing")) {
    return "Selected input is still preparing.";
  }
  if (selectedRows.some((row) => row.input_state === "stale")) {
    return "Selected input is stale. Regenerate it before generating a report.";
  }
  if (selectedRows.some((row) => row.input_state === "failed")) {
    return "Selected input failed preparation. Retry preparation before generating a report.";
  }
  if (
    selectedRows.some(
      (row) => row.input_state === "ready" && !row.current_memo?.id,
    )
  ) {
    return "Selected ready input is missing its current memo.";
  }
  if (selectedRows.some((row) => row.input_state === "not_prepared")) {
    return "Selected input needs preparation before generating a report.";
  }

  const memoIds = getSelectedReadyMemoIds(state.rows, selected);
  if (memoIds.length === 0) {
    return "Select at least one ready reporting input.";
  }
  return null;
}
