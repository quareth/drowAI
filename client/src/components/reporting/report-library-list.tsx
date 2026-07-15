/**
 * Tenant report library list.
 *
 * Responsibility: render generated report artifacts independently of live
 * engagement selection and expose a preview action for the selected report.
 */

import { AlertCircle, ChevronLeft, ChevronRight, Eye, FileText } from "lucide-react";

import { Button } from "@/components/ui/button";
import { reportTitleDisplayLabel } from "@/components/reporting/report-display-labels";
import { cn } from "@/lib/utils";
import type { ReportLibraryItem, UUIDString } from "@/types/reporting";

interface ReportLibraryListProps {
  reports: ReportLibraryItem[];
  total?: number;
  limit?: number;
  offset?: number;
  selectedReportId?: UUIDString | null;
  isLoading?: boolean;
  isFetching?: boolean;
  isError?: boolean;
  errorMessage?: string | null;
  onOpenReport: (reportId: UUIDString) => void;
  onPreviousPage?: () => void;
  onNextPage?: () => void;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "Not generated yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function sourceLabel(report: ReportLibraryItem): string {
  return [
    `${report.source_task_count} task${report.source_task_count === 1 ? "" : "s"}`,
    `${report.source_evidence_count} evidence`,
    `${report.source_knowledge_count} knowledge`,
  ].join(" · ");
}

function engagementLabel(report: ReportLibraryItem): string {
  return report.engagement_name_snapshot?.trim() || `Engagement ${report.engagement_id}`;
}

export function ReportLibraryList({
  reports,
  total = reports.length,
  limit = reports.length,
  offset = 0,
  selectedReportId = null,
  isLoading = false,
  isFetching = false,
  isError = false,
  errorMessage = null,
  onOpenReport,
  onPreviousPage,
  onNextPage,
}: ReportLibraryListProps) {
  if (isLoading) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
        <h2 className="text-base font-semibold text-slate-100">Report Library</h2>
        <p className="mt-2 text-sm text-slate-400" aria-live="polite">
          Loading generated reports...
        </p>
      </section>
    );
  }

  if (isError) {
    return (
      <section className="rounded-lg border border-rose-900/70 bg-rose-950/20 p-4">
        <div className="flex items-center gap-2 text-base font-semibold text-rose-100">
          <AlertCircle className="h-4 w-4" />
          Report Library
        </div>
        <p className="mt-2 text-sm text-rose-100" aria-live="polite">
          {errorMessage ?? "Could not load generated reports."}
        </p>
      </section>
    );
  }

  if (reports.length === 0) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
        <div className="flex items-center gap-2 text-base font-semibold text-slate-100">
          <FileText className="h-4 w-4" />
          Report Library
        </div>
        <p className="mt-2 text-sm text-slate-400" aria-live="polite">
          No generated reports yet.
        </p>
      </section>
    );
  }

  const pageStart = reports.length > 0 ? offset + 1 : 0;
  const pageEnd = offset + reports.length;
  const hasMultiplePages = total > limit;
  const canGoPrevious = offset > 0;
  const canGoNext = pageEnd < total;

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-100">Report Library</h2>
          <p className="mt-1 text-xs text-slate-500">
            Showing {pageStart}-{pageEnd} of {total} generated report
            {total === 1 ? "" : "s"}.
          </p>
        </div>
      </div>

      <ul className="mt-3 grid max-h-64 gap-2 overflow-y-auto overflow-x-hidden pr-1">
        {reports.map((report) => {
          const isSelected = selectedReportId === report.report_id;
          const displayTitle = reportTitleDisplayLabel(report.report_type, report.title);
          const metadata = [
            engagementLabel(report),
            `Version ${report.version}`,
            formatTimestamp(report.generated_at),
            sourceLabel(report),
          ];

          return (
            <li
              key={report.report_id}
              className={cn(
                "min-w-0 rounded-md border bg-slate-950/50 px-3 py-2",
                isSelected ? "border-cyan-700/70" : "border-slate-800",
              )}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-100">
                    {displayTitle}
                  </p>
                  <p className="mt-2 flex flex-wrap gap-1.5 text-xs text-slate-400">
                    {metadata.map((item) => (
                      <span
                        key={`${report.report_id}-${item}`}
                        className="max-w-full rounded border border-slate-800/80 bg-slate-900/70 px-1.5 py-0.5 leading-5"
                      >
                        {item}
                      </span>
                    ))}
                  </p>
                </div>
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
              </div>
            </li>
          );
        })}
      </ul>

      {hasMultiplePages ? (
        <div className="mt-3 flex items-center justify-between gap-3 border-t border-slate-800 pt-3">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 border-slate-800 bg-transparent px-2 text-xs text-slate-300 hover:border-slate-700 hover:bg-slate-800/70 hover:text-white"
            onClick={onPreviousPage}
            disabled={!canGoPrevious || isFetching}
          >
            <ChevronLeft className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
            Previous
          </Button>
          <span className="shrink-0 text-xs text-slate-500" aria-live="polite">
            {pageStart}-{pageEnd} / {total}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 border-slate-800 bg-transparent px-2 text-xs text-slate-300 hover:border-slate-700 hover:bg-slate-800/70 hover:text-white"
            onClick={onNextPage}
            disabled={!canGoNext || isFetching}
          >
            Next
            <ChevronRight className="ml-1 h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        </div>
      ) : null}
    </section>
  );
}
