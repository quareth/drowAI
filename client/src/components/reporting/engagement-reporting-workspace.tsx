/**
 * Engagement reporting workspace entry point.
 *
 * Responsibility: host the engagement-owned reporting experience delegated from
 * the Reports page, own top-level reporting selection state, and compose
 * high-level regions without embedding row rendering or report body rendering.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { EngagementCombobox } from "@/components/engagements/engagement-combobox";
import { fetchReportingLLMSelection } from "@/features/llm-provider/api";
import {
  invalidateReportSummaries,
  invalidateReportingInputs,
  getPrepareSelection,
  reportingKeys,
  useActiveEngagementReportJob,
  useCurrentEngagementReport,
  useDeleteEngagementReport,
  useEngagementReport,
  useEngagementReportHistory,
  useEngagementReportingInputs,
  useGenerateEngagementReport,
  usePrepareSelectedTaskMemos,
  usePrepareTaskMemo,
  useReportJobStatus,
  useUndoDeleteEngagementReport,
} from "@/hooks/use-reporting";
import { useToast } from "@/hooks/use-toast";
import { useTenantContext } from "@/hooks/use-tenant-context";
import { TENANT_ACTIONS, hasTenantAction, toTenantActionSet } from "@/lib/tenant-permissions";
import { cn } from "@/lib/utils";
import type {
  EngagementReportHistoryItem,
  EngagementReportReadResponse,
  ReportType,
  ReportingInputTaskRow,
  UUIDString,
} from "@/types/reporting";
import { Button } from "@/components/ui/button";
import {
  canSelectReportingTaskInput,
  ReportingTaskTable,
  type ReportingTaskPrepareOptions,
} from "@/components/reporting/reporting-task-table";
import { Switch } from "@/components/ui/switch";
import { ToastAction } from "@/components/ui/toast";
import { ReportActionBar } from "@/components/reporting/report-action-bar";
import { ReportHistoryList } from "@/components/reporting/report-history-list";
import { ReportJobProgress } from "@/components/reporting/report-job-progress";
import { ReportPreview } from "@/components/reporting/report-preview";
import { safeReportingMessage } from "@/components/reporting/reporting-safe-message";

const ENGAGEMENT_REPORT_TYPE: ReportType = "pentest";
const ACTIVE_REPORT_JOB_STATUSES = new Set(["queued", "generating"]);

function readEngagementIdFromLocation(): number | null {
  if (typeof window === "undefined") {
    return null;
  }

  const value = new URLSearchParams(window.location.search).get("engagement_id");
  if (!value) {
    return null;
  }

  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

export function EngagementReportingWorkspace() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { effectivePermissions } = useTenantContext();
  const tenantActions = useMemo(
    () => toTenantActionSet(effectivePermissions),
    [effectivePermissions],
  );
  const canWriteReports = hasTenantAction(tenantActions, TENANT_ACTIONS.reportWrite);
  const canDeleteReports = hasTenantAction(tenantActions, TENANT_ACTIONS.reportDelete);
  const initialEngagementId = useMemo(readEngagementIdFromLocation, []);
  const [selectedEngagementId, setSelectedEngagementId] = useState<number | null>(
    initialEngagementId,
  );
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<number>>(() => new Set());
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const [includeCandidateFindings, setIncludeCandidateFindings] = useState(false);
  const [activeReportJobId, setActiveReportJobId] = useState<string | null>(null);
  const [readyReportId, setReadyReportId] = useState<string | null>(null);
  const [selectedHistoryReportId, setSelectedHistoryReportId] = useState<string | null>(
    null,
  );
  const [handledTerminalJobId, setHandledTerminalJobId] = useState<string | null>(null);
  const [generateActionErrorMessage, setGenerateActionErrorMessage] = useState<
    string | null
  >(null);

  const inputsQuery = useEngagementReportingInputs(selectedEngagementId);
  const currentReportQuery = useCurrentEngagementReport(
    selectedEngagementId,
    ENGAGEMENT_REPORT_TYPE,
  );
  const reportingSelectionQuery = useQuery({
    queryKey: ["/api/llm/reporting-selection"],
    queryFn: fetchReportingLLMSelection,
  });
  const historyQuery = useEngagementReportHistory(
    selectedEngagementId,
    ENGAGEMENT_REPORT_TYPE,
  );
  const activeReportJobQuery = useActiveEngagementReportJob(
    selectedEngagementId,
    ENGAGEMENT_REPORT_TYPE,
    {
      enabled: selectedEngagementId !== null && activeReportJobId === null,
      refetchInterval: false,
    },
  );
  const reportJobQuery = useReportJobStatus(activeReportJobId, {
    enabled: activeReportJobId !== null,
    refetchInterval: 2000,
  });
  const readyReportQuery = useEngagementReport(readyReportId);
  const selectedHistoryReportQuery = useEngagementReport(selectedHistoryReportId);
  const prepareMemoMutation = usePrepareTaskMemo();
  const prepareSelectedMutation = usePrepareSelectedTaskMemos();
  const generateReportMutation = useGenerateEngagementReport();
  const deleteReportMutation = useDeleteEngagementReport();
  const undoDeleteReportMutation = useUndoDeleteEngagementReport();

  const taskRows = inputsQuery.data?.tasks ?? [];
  const currentReport = currentReportQuery.data?.report ?? null;
  const historyReports = historyQuery.data?.reports ?? [];
  const previewReport =
    selectedHistoryReportId !== null
      ? (selectedHistoryReportQuery.data ?? null)
      : readyReportId !== null
      ? (readyReportQuery.data ?? currentReport)
      : currentReport;
  const isPreviewLoading =
    selectedHistoryReportId !== null
      ? selectedHistoryReportQuery.isLoading
      : readyReportId !== null
      ? readyReportQuery.isLoading
      : currentReportQuery.isLoading;
  const isPreviewError =
    selectedHistoryReportId !== null
      ? selectedHistoryReportQuery.isError
      : readyReportId !== null
      ? readyReportQuery.isError
      : currentReportQuery.isError;
  const previewErrorMessage =
    selectedHistoryReportId !== null
      ? (selectedHistoryReportQuery.error?.message ?? null)
      : readyReportId !== null
      ? (readyReportQuery.error?.message ?? null)
      : (currentReportQuery.error?.message ?? null);
  const isRefreshing =
    inputsQuery.isFetching ||
    currentReportQuery.isFetching ||
    historyQuery.isFetching ||
    activeReportJobQuery.isFetching;
  const isReportJobActive =
    activeReportJobId !== null &&
    (!reportJobQuery.data || ACTIVE_REPORT_JOB_STATUSES.has(reportJobQuery.data.status));
  const reportingModelBlockReason =
    !canWriteReports
      ? "Your current tenant permissions allow report viewing only."
      : reportingSelectionQuery.isLoading
      ? "Checking reporting model status."
      : reportingSelectionQuery.data?.selectionStatus.runnable === false
      ? reportingSelectionQuery.data.selectionStatus.reason ??
        "Configure a reporting model before generating reports."
      : reportingSelectionQuery.isError
        ? "Reporting model status is unavailable."
        : null;

  useEffect(() => {
    const activeJob = activeReportJobQuery.data?.job;
    if (!activeJob || activeReportJobId !== null) {
      return;
    }
    if (!ACTIVE_REPORT_JOB_STATUSES.has(activeJob.status)) {
      return;
    }
    setActiveReportJobId(activeJob.id);
    setHandledTerminalJobId(null);
  }, [activeReportJobId, activeReportJobQuery.data?.job]);

  useEffect(() => {
    setSelectedTaskIds(new Set());
    setSelectedTaskId(null);
    setActiveReportJobId(null);
    setReadyReportId(null);
    setSelectedHistoryReportId(null);
    setHandledTerminalJobId(null);
    setGenerateActionErrorMessage(null);
  }, [selectedEngagementId]);

  useEffect(() => {
    setGenerateActionErrorMessage(null);
  }, [selectedTaskIds]);

  useEffect(() => {
    if (
      selectedHistoryReportId !== null &&
      historyQuery.isSuccess &&
      !historyReports.some((report) => report.report_id === selectedHistoryReportId)
    ) {
      setSelectedHistoryReportId(null);
    }
  }, [historyQuery.isSuccess, historyReports, selectedHistoryReportId]);

  useEffect(() => {
    const job = reportJobQuery.data;
    if (!job || job.id === handledTerminalJobId) {
      return;
    }
    if (job.status !== "ready" && job.status !== "failed" && job.status !== "cancelled") {
      return;
    }

    setHandledTerminalJobId(job.id);
    if (job.status === "ready") {
      if (job.report_id) {
        setReadyReportId(job.report_id);
        setSelectedHistoryReportId(null);
      }
      void invalidateReportSummaries(queryClient, job.engagement_id, job.report_type);
    }
  }, [handledTerminalJobId, queryClient, reportJobQuery.data]);

  useEffect(() => {
    if (taskRows.length === 0) {
      setSelectedTaskIds((current) => (current.size === 0 ? current : new Set()));
      setSelectedTaskId(null);
      return;
    }

    const availableTaskIds = new Set(taskRows.map((row) => row.task_id));
    const selectableTaskIds = new Set(
      taskRows.filter(canSelectReportingTaskInput).map((row) => row.task_id),
    );
    setSelectedTaskIds((current) => {
      const next = new Set(
        [...current].filter((taskId) => selectableTaskIds.has(taskId)),
      );
      return next.size === current.size ? current : next;
    });
    setSelectedTaskId((current) =>
      current !== null && availableTaskIds.has(current) ? current : null,
    );
  }, [taskRows]);

  const handleRefresh = useCallback(() => {
    if (selectedEngagementId === null) {
      return;
    }

    void Promise.all([
      invalidateReportingInputs(queryClient, selectedEngagementId),
      invalidateReportSummaries(queryClient, selectedEngagementId, ENGAGEMENT_REPORT_TYPE),
      queryClient.invalidateQueries({
        queryKey: reportingKeys.activeJob(selectedEngagementId, ENGAGEMENT_REPORT_TYPE),
      }),
    ]);
  }, [queryClient, selectedEngagementId]);

  const preparingTaskIds = useMemo(() => {
    const taskIds = new Set(prepareSelectedMutation.preparingTaskIds);
    const taskId = prepareMemoMutation.variables?.task_id;
    if (prepareMemoMutation.isPending && taskId) {
      taskIds.add(taskId);
    }
    return taskIds;
  }, [
    prepareMemoMutation.isPending,
    prepareMemoMutation.variables?.task_id,
    prepareSelectedMutation.preparingTaskIds,
  ]);

  const handlePrepareTask = useCallback(
    (row: ReportingInputTaskRow, options: ReportingTaskPrepareOptions) => {
      if (reportingModelBlockReason) {
        return;
      }
      prepareMemoMutation.mutate({
        task_id: row.task_id,
        engagement_id: selectedEngagementId,
        regenerate: options.regenerate,
      }, {
        onError: (error) => {
          toast({
            title: "Preparation blocked",
            description:
              safeReportingMessage(
                error.message,
                "Backend rejected the action. Review the selected input and try again.",
              ) ?? "Memo preparation failed.",
            variant: "destructive",
          });
        },
      });
    },
    [prepareMemoMutation, reportingModelBlockReason, selectedEngagementId, toast],
  );

  const handlePrepareSelected = useCallback(() => {
    if (
      selectedEngagementId === null ||
      prepareSelectedMutation.isPending ||
      reportingModelBlockReason
    ) {
      return;
    }
    const rows = getPrepareSelection(taskRows, selectedTaskIds);
    if (rows.length === 0) {
      return;
    }
    prepareSelectedMutation.mutate({
      engagement_id: selectedEngagementId,
      rows,
    });
  }, [
    prepareSelectedMutation,
    reportingModelBlockReason,
    selectedEngagementId,
    selectedTaskIds,
    taskRows,
  ]);

  const handleGenerateReport = useCallback((selectedMemoIds: UUIDString[]) => {
    if (generateReportMutation.isPending) {
      return;
    }
    try {
      if (reportingModelBlockReason) {
        setGenerateActionErrorMessage(reportingModelBlockReason);
        return;
      }
      if (selectedEngagementId === null) {
        setGenerateActionErrorMessage("Select an engagement to generate a report.");
        return;
      }
      if (selectedMemoIds.length === 0) {
        setGenerateActionErrorMessage(
          "Select at least one ready reporting input before generating a report.",
        );
        return;
      }
      setGenerateActionErrorMessage(null);
      generateReportMutation.reset();
      setActiveReportJobId(null);
      setReadyReportId(null);
      setSelectedHistoryReportId(null);
      setHandledTerminalJobId(null);
      generateReportMutation.mutate(
        {
          engagement_id: selectedEngagementId,
          report_type: ENGAGEMENT_REPORT_TYPE,
          selected_task_memo_ids: selectedMemoIds,
          include_candidate_findings: includeCandidateFindings,
          force_regenerate: currentReport !== null,
        },
        {
          onSuccess: (response) => {
            setActiveReportJobId(response.job_id);
            setReadyReportId(response.status === "ready" ? response.report_id : null);
            setHandledTerminalJobId(null);
            setGenerateActionErrorMessage(null);
          },
        },
      );
    } catch {
      setGenerateActionErrorMessage(
        "Could not start report generation. Refresh reporting inputs and try again.",
      );
    }
  }, [
    generateReportMutation,
    includeCandidateFindings,
    currentReport,
    reportingModelBlockReason,
    selectedEngagementId,
  ]);

  const handleOpenHistoryReport = useCallback((reportId: string) => {
    setSelectedHistoryReportId(reportId);
  }, []);

  const handleUndoDeleteReport = useCallback(
    async (reportId: string) => {
      try {
        const response = await undoDeleteReportMutation.mutateAsync({
          report_id: reportId,
          engagement_id: selectedEngagementId,
          report_type: ENGAGEMENT_REPORT_TYPE,
        });
        if (response.restored_current || response.current_report_id === reportId) {
          setSelectedHistoryReportId(null);
        }
        toast({
          title: "Report restored",
          description: "The report deletion was undone.",
        });
      } catch (error) {
        toast({
          title: "Undo failed",
          description:
            error instanceof Error ? error.message : "Could not restore the report.",
          variant: "destructive",
        });
      }
    },
    [selectedEngagementId, toast, undoDeleteReportMutation],
  );

  const handleDeleteReport = useCallback(
    async (
      report:
        | EngagementReportReadResponse
        | EngagementReportHistoryItem,
    ) => {
      const reportId = "report_id" in report ? report.report_id : report.id;
      try {
        const response = await deleteReportMutation.mutateAsync({
          report_id: reportId,
          engagement_id: selectedEngagementId,
          report_type: ENGAGEMENT_REPORT_TYPE,
        });
        if (selectedHistoryReportId === reportId) {
          setSelectedHistoryReportId(null);
        }
        if (readyReportId === reportId) {
          setReadyReportId(response.current_report_id);
        }
        toast({
          title: "Report deleted",
          description: "The generated report was removed from history.",
          action: (
            <ToastAction
              altText="Undo report deletion"
              onClick={() => void handleUndoDeleteReport(reportId)}
            >
              Undo
            </ToastAction>
          ),
        });
      } catch (error) {
        toast({
          title: "Delete failed",
          description:
            error instanceof Error ? error.message : "Could not delete the report.",
          variant: "destructive",
        });
      }
    },
    [
      deleteReportMutation,
      handleUndoDeleteReport,
      readyReportId,
      selectedEngagementId,
      selectedHistoryReportId,
      toast,
    ],
  );

  return (
    <main className="flex-1 overflow-auto bg-slate-950 p-4 md:p-6">
      <div className="mx-auto flex min-h-full max-w-7xl flex-col gap-4">
        <header>
          <div>
            <h1 className="text-3xl font-bold text-white">Reports</h1>
          </div>
        </header>

        <section className="rounded-lg border border-slate-900 bg-slate-950/30 p-2">
          <div className="grid gap-3 md:grid-cols-[minmax(260px,1fr)_auto_auto] md:items-start">
            <div className="grid gap-1 text-xs font-medium text-slate-300">
              <span>Engagement</span>
              <EngagementCombobox
                value={selectedEngagementId}
                onChange={setSelectedEngagementId}
                allowCreate={false}
                allowNone={false}
                ariaLabel="Engagement"
                helperText={null}
              />
            </div>

            <div className="grid gap-1 text-xs font-medium text-slate-300 md:pt-5">
              <label className="flex h-10 items-center justify-between gap-3 rounded-md border border-slate-800 bg-slate-950 px-3 text-xs text-slate-300">
                <span className="whitespace-nowrap">Low-confidence findings</span>
                <Switch
                  checked={includeCandidateFindings}
                  onCheckedChange={setIncludeCandidateFindings}
                  aria-label="Include low-confidence candidate findings"
                />
              </label>
            </div>
            <div className="grid gap-1 text-xs font-medium text-slate-300 md:pt-5">
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="h-10 w-10 border-slate-800 bg-slate-950 text-slate-300 hover:border-slate-700 hover:bg-slate-900 hover:text-white"
                onClick={handleRefresh}
                disabled={selectedEngagementId === null || isRefreshing}
                aria-label="Refresh reporting workspace"
                title="Refresh reporting workspace"
              >
                <RefreshCw className={cn("h-4 w-4", isRefreshing && "animate-spin")} />
              </Button>
            </div>
          </div>
        </section>

        {selectedEngagementId === null ? (
          <section className="flex min-h-[360px] items-center justify-center rounded-lg border border-dashed border-slate-800 bg-slate-900/50 p-8 text-center">
            <div className="max-w-md">
              <h2 className="text-xl font-semibold text-slate-100">Select an engagement</h2>
              <p className="mt-2 text-sm text-slate-400">
                Reporting inputs, current report state, and history load after an
                engagement is selected.
              </p>
            </div>
          </section>
        ) : (
          <section className="grid min-h-[520px] gap-4 lg:grid-cols-[minmax(0,1.55fr)_minmax(360px,0.85fr)]">
            <section className="min-w-0 rounded-lg border border-slate-800 bg-slate-900/70 p-4">
              <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <h2 className="text-lg font-semibold text-slate-100">
                    Task Inputs · {taskRows.length} input
                    {taskRows.length === 1 ? "" : "s"}
                  </h2>
                </div>
              </div>

              <div className="mt-4">
                <ReportActionBar
                  rows={taskRows}
                  selectedTaskIds={selectedTaskIds}
                  selectedEngagementId={selectedEngagementId}
                  reportType={ENGAGEMENT_REPORT_TYPE}
                  hasCurrentReport={currentReport !== null}
                  isPreparingSelected={prepareSelectedMutation.isPending}
                  isGeneratingReport={
                    generateReportMutation.isPending || isReportJobActive
                  }
                  reportingModelBlockReason={reportingModelBlockReason}
                  generationErrorMessage={
                    generateActionErrorMessage ??
                    generateReportMutation.error?.message ??
                    null
                  }
                  prepareProgress={prepareSelectedMutation.progress}
                  prepareResults={prepareSelectedMutation.data?.results ?? []}
                  onPrepareSelected={handlePrepareSelected}
                  onGenerateReport={handleGenerateReport}
                />
              </div>

              <div className="mt-3">
                <ReportingTaskTable
                  tasks={taskRows}
                  selectedTaskIds={selectedTaskIds}
                  onSelectedTaskIdsChange={setSelectedTaskIds}
                  activeTaskId={selectedTaskId}
                  onActiveTaskIdChange={setSelectedTaskId}
                  onPrepareTask={handlePrepareTask}
                  preparingTaskIds={preparingTaskIds}
                  prepareDisabledReason={reportingModelBlockReason}
                  isLoading={inputsQuery.isLoading}
                  isError={inputsQuery.isError}
                />
              </div>
            </section>

            <aside className="grid min-w-0 gap-4">
              <ReportJobProgress
                job={reportJobQuery.data ?? null}
                isSubmitting={generateReportMutation.isPending}
                isLoading={
                  activeReportJobQuery.isLoading ||
                  (activeReportJobId !== null && reportJobQuery.isLoading)
                }
                isError={activeReportJobQuery.isError || reportJobQuery.isError}
                errorMessage={
                  activeReportJobQuery.error?.message ??
                  reportJobQuery.error?.message ??
                  null
                }
              />

              <ReportPreview
                report={previewReport}
                isLoading={isPreviewLoading}
                isError={isPreviewError}
                errorMessage={previewErrorMessage}
                isDeleting={
                  previewReport !== null &&
                  deleteReportMutation.isPending &&
                  deleteReportMutation.variables?.report_id === previewReport.id
                }
                onDeleteReport={canDeleteReports ? handleDeleteReport : undefined}
              />

              <ReportHistoryList
                reports={historyReports}
                selectedReportId={selectedHistoryReportId}
                isLoading={historyQuery.isLoading}
                isError={historyQuery.isError}
                errorMessage={historyQuery.error?.message ?? null}
                onOpenReport={handleOpenHistoryReport}
                onDeleteReport={canDeleteReports ? handleDeleteReport : undefined}
                deletingReportId={
                  deleteReportMutation.isPending
                    ? deleteReportMutation.variables?.report_id ?? null
                    : null
                }
              />
            </aside>
          </section>
        )}
      </div>
    </main>
  );
}
