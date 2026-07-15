/**
 * Reporting workspace action bar for selected task input actions.
 *
 * Responsibility: render selected-input batch controls and preparation progress
 * without owning reporting API calls or task row rendering.
 */

import { AlertTriangle, FileCheck2, FileText, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  getGenerateDisabledReason,
  getPrepareDisabledReason,
  getPrepareSelection,
  getSelectedReadyMemoIds,
  type PrepareSelectedTaskMemoResult,
  type PrepareSelectedTaskMemosProgress,
} from "@/hooks/use-reporting";
import { cn } from "@/lib/utils";
import type { ReportType, ReportingInputTaskRow } from "@/types/reporting";
import { safeReportingMessage } from "@/components/reporting/reporting-safe-message";

interface ReportActionBarProps {
  rows: ReportingInputTaskRow[];
  selectedTaskIds: Set<number>;
  selectedEngagementId?: number | null;
  reportType: ReportType;
  hasCurrentReport?: boolean;
  isPreparingSelected?: boolean;
  isGeneratingReport?: boolean;
  reportingModelBlockReason?: string | null;
  generationErrorMessage?: string | null;
  prepareProgress?: PrepareSelectedTaskMemosProgress | null;
  prepareResults?: PrepareSelectedTaskMemoResult[];
  onPrepareSelected: () => void;
  onGenerateReport: (selectedMemoIds: string[]) => void;
}

function formatSelectionCount(count: number): string {
  return `${count} selected input${count === 1 ? "" : "s"}`;
}

function progressText(
  progress: PrepareSelectedTaskMemosProgress | null | undefined,
): string | null {
  if (!progress || progress.total === 0) {
    return null;
  }
  if (progress.completed < progress.total) {
    return `Preparing ${progress.completed} of ${progress.total} selected inputs...`;
  }
  if (progress.failed > 0) {
    return `Prepared ${progress.completed - progress.failed} of ${progress.total}; ${progress.failed} need retry.`;
  }
  return `Prepared ${progress.total} selected input${progress.total === 1 ? "" : "s"}.`;
}

export function ReportActionBar({
  rows,
  selectedTaskIds,
  selectedEngagementId = null,
  reportType,
  hasCurrentReport = false,
  isPreparingSelected = false,
  isGeneratingReport = false,
  reportingModelBlockReason = null,
  generationErrorMessage = null,
  prepareProgress = null,
  prepareResults = [],
  onPrepareSelected,
  onGenerateReport,
}: ReportActionBarProps) {
  const preparableRows = getPrepareSelection(rows, selectedTaskIds);
  const readyMemoIds = getSelectedReadyMemoIds(rows, selectedTaskIds);
  const selectedCount = selectedTaskIds.size;
  const preparableCount = preparableRows.length;
  const failures = prepareResults.filter((result) => !result.ok);
  const prepareDisabledReason =
    reportingModelBlockReason ??
    getPrepareDisabledReason({
      rows,
      selectedTaskIds,
      engagementId: selectedEngagementId,
      isPreparingSelected,
    });
  const generateDisabledReason =
    reportingModelBlockReason ??
    getGenerateDisabledReason({
      rows,
      selectedTaskIds,
      engagementId: selectedEngagementId,
      reportType,
      isGenerating: isGeneratingReport,
    });
  const safeGenerationError = safeReportingMessage(
    generationErrorMessage,
    "Backend rejected the action. Review the selected inputs and permissions.",
  );
  const prepareDisabled = prepareDisabledReason !== null;
  const generateDisabled = generateDisabledReason !== null;
  const selectedLabel = formatSelectionCount(selectedCount);
  const statusText =
    progressText(prepareProgress) ??
    prepareDisabledReason ??
    (preparableCount > 0
      ? `${preparableCount} can be prepared.`
      : "Select inputs that need preparation.");
  const generateSummary =
    safeGenerationError ??
    generateDisabledReason ??
    `${readyMemoIds.length} ready input${readyMemoIds.length === 1 ? "" : "s"} can ${
      hasCurrentReport ? "generate a new report version" : "generate a report"
    }.`;
  const generateLabel = hasCurrentReport ? "Generate New Report" : "Generate Report";
  const shouldShowGenerateSummary =
    safeGenerationError !== null ||
    (selectedCount > 0 && generateDisabledReason !== null) ||
    readyMemoIds.length > 0;
  const shouldShowStatusText =
    selectedCount > 0 || prepareProgress !== null || failures.length > 0;

  return (
    <div className="rounded-lg border border-slate-900 bg-slate-950/30 p-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="min-w-0">
          <div className="text-sm font-medium text-slate-100">{selectedLabel}</div>
          {shouldShowStatusText ? (
            <div
              className={cn(
                "mt-1 text-xs",
                failures.length > 0 ? "text-amber-200" : "text-slate-400",
              )}
              aria-live="polite"
            >
              {statusText}
            </div>
          ) : null}
        </div>

        <div className="flex w-full flex-col gap-2 sm:flex-row md:w-auto">
          <Button
            type="button"
            variant="outline"
            size="icon"
            onClick={onPrepareSelected}
            disabled={prepareDisabled}
            title={prepareDisabledReason ?? "Prepare selected inputs"}
            aria-label="Prepare Selected"
            className="h-8 w-8 border-slate-800 bg-transparent text-slate-300 hover:border-emerald-800 hover:bg-emerald-950/50 hover:text-emerald-100 disabled:text-slate-500"
          >
            {isPreparingSelected ? (
              <RefreshCw className="h-4 w-4 animate-spin" />
            ) : (
              <FileCheck2 className="h-4 w-4" />
            )}
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={() => onGenerateReport(readyMemoIds)}
            disabled={generateDisabled}
            title={
              generateDisabledReason ??
              (hasCurrentReport
                ? "Generate a new report version from selected inputs"
                : "Generate report from selected inputs")
            }
            className="h-8 w-full bg-cyan-600/90 px-3 text-xs text-white hover:bg-cyan-500 disabled:bg-slate-800 disabled:text-slate-500 sm:w-auto"
          >
            {isGeneratingReport ? (
              <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <FileText className="mr-2 h-4 w-4" />
            )}
            {generateLabel}
          </Button>
        </div>
      </div>

      {shouldShowGenerateSummary ? (
        <div
          className={cn(
            "mt-3 rounded-md border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs",
            safeGenerationError
              ? "text-amber-200"
              : generateDisabledReason
                ? "text-slate-400"
                : "text-emerald-200",
          )}
          aria-live="polite"
        >
          {generateSummary}
        </div>
      ) : null}

      {failures.length > 0 ? (
        <div className="mt-3 rounded-md border border-amber-900/70 bg-amber-950/20 p-2 text-xs text-amber-100">
          <div className="flex items-center gap-2 font-medium">
            <AlertTriangle className="h-3.5 w-3.5" />
            Some inputs need another preparation attempt.
          </div>
          <ul className="mt-2 space-y-1">
            {failures.slice(0, 3).map((failure) => (
              <li key={failure.task_id} className="line-clamp-2">
                <span className="font-medium">{failure.task_name}</span>:{" "}
                {safeReportingMessage(
                  failure.error_message,
                  "Backend rejected the action. Review the selected inputs and permissions.",
                ) ??
                  "Memo preparation failed."}
              </li>
            ))}
          </ul>
          {failures.length > 3 ? (
            <div className="mt-1 text-amber-200">
              {failures.length - 3} more input
              {failures.length - 3 === 1 ? "" : "s"} can be retried.
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
