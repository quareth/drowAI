/**
 * Reporting input task table for engagement-owned report preparation.
 *
 * Responsibility: render task input inventory rows, derive row selection
 * eligibility, and surface compact reasons without owning API mutations.
 */

import { FileCheck2, RefreshCw } from "lucide-react";

import { ReportingStatusBadge } from "@/components/reporting/reporting-status-badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { canPrepareReportingInput, canSelectInputForGeneration } from "@/hooks/use-reporting";
import { cn } from "@/lib/utils";
import type { ReportingInputTaskRow, ReportingReasonCode } from "@/types/reporting";

type SelectionPurpose = "generation" | "preparation";

export interface ReportingTaskSelectionState {
  selectable: boolean;
  purpose: SelectionPurpose | null;
  disabledReason: string | null;
}

export interface ReportingTaskPrepareOptions {
  regenerate: boolean;
}

interface ReportingTaskTableProps {
  tasks: ReportingInputTaskRow[];
  selectedTaskIds: Set<number>;
  onSelectedTaskIdsChange: (selectedTaskIds: Set<number>) => void;
  activeTaskId?: number | null;
  onActiveTaskIdChange?: (taskId: number) => void;
  onPrepareTask?: (
    row: ReportingInputTaskRow,
    options: ReportingTaskPrepareOptions,
  ) => void;
  preparingTaskIds?: Set<number>;
  prepareDisabledReason?: string | null;
  isLoading?: boolean;
  isError?: boolean;
}

const NOT_PREPARABLE_REASON_LABELS: Record<ReportingReasonCode, string> = {
  task_not_stopped: "Stop this task before preparing input.",
  runtime_retirement_not_confirmed: "Wait for runtime retirement confirmation.",
  no_useful_runtime_execution: "No useful runtime activity is available.",
  no_reportable_or_limited_source_material:
    "No reportable source material is available.",
};

function formatTaskStatus(status: string): string {
  const normalized = status.trim();
  if (!normalized) {
    return "Unknown";
  }
  return normalized
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function formatTime(value: string | null | undefined): string {
  if (!value) {
    return "Not generated";
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    return "Unavailable";
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

function memoVersion(row: ReportingInputTaskRow): string {
  if (!row.current_memo) {
    return "None";
  }
  return `v${row.current_memo.version}`;
}

function latestMemoTime(row: ReportingInputTaskRow): string {
  return formatTime(
    row.current_memo?.generated_at ??
      row.latest_memo_attempt?.generated_at ??
      row.latest_memo_attempt?.updated_at,
  );
}

function failedAttemptError(row: ReportingInputTaskRow): string | null {
  if (row.input_state !== "failed") {
    return null;
  }
  const message = row.latest_memo_attempt?.error_message?.trim();
  return message || "Latest memo attempt failed.";
}

function disabledReasonFor(row: ReportingInputTaskRow): string {
  if (row.input_state === "preparing") {
    return "Memo preparation is in progress.";
  }
  if (row.input_state === "ready" && !row.current_memo?.id) {
    return "Ready input is missing a current memo.";
  }
  if (row.not_preparable_reason) {
    return NOT_PREPARABLE_REASON_LABELS[row.not_preparable_reason];
  }
  if (!row.runtime_retired) {
    return "Stop this task before preparing input.";
  }
  return "This input is not eligible yet.";
}

export function getReportingTaskSelectionState(
  row: ReportingInputTaskRow,
): ReportingTaskSelectionState {
  if (canSelectInputForGeneration(row)) {
    return {
      selectable: true,
      purpose: "generation",
      disabledReason: null,
    };
  }
  if (canPrepareReportingInput(row)) {
    return {
      selectable: true,
      purpose: "preparation",
      disabledReason: null,
    };
  }
  return {
    selectable: false,
    purpose: null,
    disabledReason: disabledReasonFor(row),
  };
}

export function canSelectReportingTaskInput(row: ReportingInputTaskRow): boolean {
  return getReportingTaskSelectionState(row).selectable;
}

function actionLabel(row: ReportingInputTaskRow): string {
  if (row.input_state === "stale" || row.input_state === "failed") {
    return "Regenerate";
  }
  return "Prepare";
}

export function ReportingTaskTable({
  tasks,
  selectedTaskIds,
  onSelectedTaskIdsChange,
  activeTaskId = null,
  onActiveTaskIdChange,
  onPrepareTask,
  preparingTaskIds = new Set(),
  prepareDisabledReason = null,
  isLoading = false,
  isError = false,
}: ReportingTaskTableProps) {
  if (isLoading) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-950/70 p-4 text-sm text-slate-400">
        Loading reporting inputs...
      </div>
    );
  }

  if (isError) {
    return (
      <div className="rounded-lg border border-rose-900/70 bg-rose-950/20 p-4 text-sm text-rose-200">
        Could not load reporting inputs.
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-800 bg-slate-950/70 p-4 text-sm text-slate-400">
        No reporting inputs are available for this engagement.
      </div>
    );
  }

  const toggleSelection = (row: ReportingInputTaskRow, checked: boolean) => {
    const selection = getReportingTaskSelectionState(row);
    if (!selection.selectable) {
      return;
    }
    const next = new Set(selectedTaskIds);
    if (checked) {
      next.add(row.task_id);
    } else {
      next.delete(row.task_id);
    }
    onSelectedTaskIdsChange(next);
  };

  return (
    <div className="w-full max-w-full overflow-hidden rounded-lg border border-slate-800 bg-slate-950/70">
      <div className="w-full max-w-full overflow-x-auto">
        <Table className="min-w-[980px] table-fixed text-xs">
          <TableHeader className="sticky top-0 z-10 bg-slate-950/95">
            <TableRow className="border-slate-800 hover:bg-transparent">
              <TableHead className="w-10 text-slate-400">Select</TableHead>
              <TableHead className="w-[210px] text-slate-400">Task</TableHead>
              <TableHead className="w-24 text-slate-400">Status</TableHead>
              <TableHead className="w-28 text-slate-400">Input</TableHead>
              <TableHead className="w-16 text-right text-slate-400">Evidence</TableHead>
              <TableHead className="w-20 text-right text-slate-400">Findings</TableHead>
              <TableHead className="w-24 text-right text-slate-400">Candidates</TableHead>
              <TableHead className="w-20 text-slate-400">Memo</TableHead>
              <TableHead className="w-32 text-slate-400">Prepared</TableHead>
              <TableHead className="w-32 text-slate-400">Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tasks.map((row) => {
            const selection = getReportingTaskSelectionState(row);
            const isSelected = selectedTaskIds.has(row.task_id);
            const isPreparing = preparingTaskIds.has(row.task_id);
            const canPrepare = canPrepareReportingInput(row) && !prepareDisabledReason;
            const errorMessage = failedAttemptError(row);
            const prepareLabel = actionLabel(row);
            const regenerate = prepareLabel === "Regenerate";

            return (
              <TableRow
                key={row.task_id}
                className={cn(
                  "border-slate-800 align-top hover:bg-slate-900/70",
                  activeTaskId === row.task_id && "bg-slate-800/50",
                )}
                onClick={() => onActiveTaskIdChange?.(row.task_id)}
              >
                <TableCell className="pt-3">
                  <Checkbox
                    checked={isSelected}
                    disabled={!selection.selectable}
                    onCheckedChange={(checked) => toggleSelection(row, checked === true)}
                    aria-label={`Select ${row.task_name}`}
                    title={selection.disabledReason ?? `Selectable for ${selection.purpose}`}
                    className="border-slate-600 data-[state=checked]:border-emerald-500 data-[state=checked]:bg-emerald-600"
                    onClick={(event) => event.stopPropagation()}
                  />
                </TableCell>
                <TableCell className="max-w-[210px]">
                  <div className="min-w-0">
                    <div
                      className="truncate font-medium text-slate-100"
                      title={row.task_name}
                    >
                      {row.task_name}
                    </div>
                    {selection.disabledReason ? (
                      <div className="mt-1 line-clamp-2 text-[11px] leading-snug text-slate-500">
                        {selection.disabledReason}
                      </div>
                    ) : null}
                    {errorMessage ? (
                      <div
                        className="mt-1 line-clamp-2 text-[11px] leading-snug text-rose-200"
                        title={errorMessage}
                      >
                        {errorMessage}
                      </div>
                    ) : null}
                  </div>
                </TableCell>
                <TableCell className="text-slate-300">
                  {formatTaskStatus(row.task_status)}
                </TableCell>
                <TableCell>
                  <ReportingStatusBadge inputState={row.input_state} />
                </TableCell>
                <TableCell className="text-right tabular-nums text-slate-300">
                  {formatCount(row.counts.evidence)}
                </TableCell>
                <TableCell className="text-right tabular-nums text-slate-300">
                  {formatCount(row.counts.canonical_findings)}
                </TableCell>
                <TableCell className="text-right tabular-nums text-slate-300">
                  {formatCount(row.counts.candidate_findings)}
                </TableCell>
                <TableCell className="text-slate-300">{memoVersion(row)}</TableCell>
                <TableCell className="text-slate-400">{latestMemoTime(row)}</TableCell>
                <TableCell>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    disabled={!canPrepare || isPreparing || !onPrepareTask}
                    onClick={(event) => {
                      event.stopPropagation();
                      onPrepareTask?.(row, { regenerate });
                    }}
                    className="h-7 w-7 border-slate-800 bg-transparent text-slate-300 hover:border-slate-700 hover:bg-slate-800/70 hover:text-white disabled:text-slate-500"
                    aria-label={`${prepareLabel} ${row.task_name}`}
                    title={prepareDisabledReason ?? `${prepareLabel} ${row.task_name}`}
                  >
                    {isPreparing ? (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                    ) : regenerate ? (
                      <RefreshCw className="h-3.5 w-3.5" />
                    ) : (
                      <FileCheck2 className="h-3.5 w-3.5" />
                    )}
                  </Button>
                </TableCell>
              </TableRow>
            );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
