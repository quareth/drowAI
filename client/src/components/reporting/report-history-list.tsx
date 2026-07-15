/**
 * Historical engagement report list.
 *
 * Responsibility: render report history summaries and expose a preview action
 * without owning report detail fetching or backend mutation behavior.
 */

import { AlertCircle, Clock, Eye, FileText, Trash2, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  reportTitleDisplayLabel,
} from "@/components/reporting/report-display-labels";
import { cn } from "@/lib/utils";
import type {
  EngagementReportHistoryItem,
  ReportStatus,
  UUIDString,
} from "@/types/reporting";

interface ReportHistoryListProps {
  reports: EngagementReportHistoryItem[];
  selectedReportId?: UUIDString | null;
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string | null;
  onOpenReport: (reportId: UUIDString) => void;
  onDeleteReport?: (report: EngagementReportHistoryItem) => void;
  deletingReportId?: UUIDString | null;
}

type StatusContent = {
  label: string;
  className: string;
  icon: typeof Clock;
};

type VisibleReportStatus = Exclude<ReportStatus, "ready">;

const STATUS_CONTENT: Record<VisibleReportStatus, StatusContent> = {
  generating: {
    label: "Generating",
    className: "border-cyan-700/70 bg-cyan-950/60 text-cyan-200",
    icon: Clock,
  },
  failed: {
    label: "Failed",
    className: "border-rose-700/70 bg-rose-950/60 text-rose-200",
    icon: XCircle,
  },
};

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "Not generated yet";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function sourceCountLabel(count: number): string {
  return `${count} task${count === 1 ? "" : "s"}`;
}

function safeFailureMessage(message: string | null | undefined): string | null {
  const trimmed = message?.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return "Report generation failed.";
  }
  return trimmed
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer <redacted>")
    .replace(/\b(token|api[_-]?key|password|secret)=\S+/gi, "$1=<redacted>");
}

export function ReportHistoryList({
  reports,
  selectedReportId = null,
  isLoading = false,
  isError = false,
  errorMessage = null,
  onOpenReport,
  onDeleteReport,
  deletingReportId = null,
}: ReportHistoryListProps) {
  const historicalReports = reports.filter((report) => !report.is_current);

  if (isLoading) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
        <h2 className="text-base font-semibold text-slate-100">History</h2>
        <p className="mt-2 text-sm text-slate-400" aria-live="polite">
          Loading report history...
        </p>
      </section>
    );
  }

  if (isError) {
    return (
      <section className="rounded-lg border border-rose-900/70 bg-rose-950/20 p-4">
        <div className="flex items-center gap-2 text-base font-semibold text-rose-100">
          <AlertCircle className="h-4 w-4" />
          History
        </div>
        <p className="mt-2 text-sm text-rose-100" aria-live="polite">
          {errorMessage ?? "Could not load report history."}
        </p>
      </section>
    );
  }

  if (historicalReports.length === 0) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
        <div className="flex items-center gap-2 text-base font-semibold text-slate-100">
          <FileText className="h-4 w-4" />
          History
        </div>
        <p className="mt-2 text-sm text-slate-400" aria-live="polite">
          No previous reports.
        </p>
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-100">History</h2>
          <p className="mt-1 text-xs text-slate-500">
            {historicalReports.length} previous report
            {historicalReports.length === 1 ? "" : "s"}.
          </p>
        </div>
      </div>

      <ul className="mt-3 grid max-h-80 gap-2 overflow-y-auto pr-1">
        {historicalReports.map((report) => {
          const isSelected = selectedReportId === report.report_id;
          const failureMessage = safeFailureMessage(report.error_message);
          const displayTitle = reportTitleDisplayLabel(report.report_type, report.title);
          const statusContent =
            report.status !== "ready" ? STATUS_CONTENT[report.status] : null;
          const showStatusBadge = statusContent !== null;
          const StatusIcon = statusContent?.icon;
          const metadata = [
            `Version ${report.version}`,
            formatTimestamp(report.generated_at),
            sourceCountLabel(report.source_task_memo_ids.length),
          ];

          return (
            <li
              key={report.report_id}
              className={cn(
                "rounded-md border bg-slate-950/50 px-3 py-2",
                isSelected ? "border-cyan-700/70" : "border-slate-800",
              )}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-xs text-slate-300">
                    {metadata.map((item, index) => (
                      <span key={`${report.report_id}-${item}`}>
                        {index > 0 ? <span className="px-1.5 text-slate-600">·</span> : null}
                        {item}
                      </span>
                    ))}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {showStatusBadge ? (
                    <Badge
                      variant="outline"
                      className={cn(
                        "h-6 w-fit max-w-full whitespace-nowrap px-2 py-0 text-[11px] font-medium leading-none",
                        statusContent?.className,
                      )}
                    >
                      {StatusIcon ? <StatusIcon className="mr-1.5 h-3.5 w-3.5" /> : null}
                      {statusContent?.label}
                    </Badge>
                  ) : null}
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    className={cn(
                      "h-7 w-7 border-slate-800 bg-transparent text-slate-300 hover:border-slate-700 hover:bg-slate-800/70 hover:text-white",
                      isSelected && "border-cyan-700/70 text-cyan-200",
                    )}
                    onClick={() => onOpenReport(report.report_id)}
                    disabled={isSelected}
                    aria-label={`Open report ${displayTitle} version ${report.version}`}
                    title={isSelected ? "Previewing report" : "Open preview"}
                  >
                    <Eye className="h-3.5 w-3.5" aria-hidden="true" />
                  </Button>
                  {onDeleteReport ? (
                    <Button
                      type="button"
                      variant="outline"
                      size="icon"
                      className="h-7 w-7 border-slate-800 bg-transparent text-slate-300 hover:border-rose-800 hover:bg-rose-950/40 hover:text-rose-100"
                      onClick={() => onDeleteReport(report)}
                      disabled={deletingReportId === report.report_id}
                      aria-label={`Delete report ${displayTitle} version ${report.version}`}
                      title="Delete report"
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                    </Button>
                  ) : null}
                </div>
              </div>

              {failureMessage ? (
                <p className="mt-2 rounded-md border border-rose-900/70 bg-rose-950/20 px-3 py-2 text-xs text-rose-100">
                  {failureMessage}
                </p>
              ) : null}

            </li>
          );
        })}
      </ul>
    </section>
  );
}
