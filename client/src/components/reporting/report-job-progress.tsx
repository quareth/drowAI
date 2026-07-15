/**
 * Report generation job progress panel.
 *
 * Responsibility: render typed report job status details and retry guidance
 * without owning reporting API calls or task input selection state.
 */

import { AlertTriangle, CheckCircle2, Clock, Loader2, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import type { EngagementReportJobStatusResponse, ReportJobStatus } from "@/types/reporting";
import { safeReportingMessage } from "@/components/reporting/reporting-safe-message";

interface ReportJobProgressProps {
  job: EngagementReportJobStatusResponse | null;
  isSubmitting?: boolean;
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string | null;
}

type StatusContent = {
  label: string;
  className: string;
  icon: typeof Clock;
};

const STATUS_CONTENT: Record<ReportJobStatus, StatusContent> = {
  queued: {
    label: "Queued",
    className: "border-sky-700/70 bg-sky-950/60 text-sky-200",
    icon: Clock,
  },
  generating: {
    label: "Generating",
    className: "border-cyan-700/70 bg-cyan-950/60 text-cyan-200",
    icon: Loader2,
  },
  ready: {
    label: "Ready",
    className: "border-emerald-700/70 bg-emerald-950/60 text-emerald-200",
    icon: CheckCircle2,
  },
  failed: {
    label: "Failed",
    className: "border-rose-700/70 bg-rose-950/60 text-rose-200",
    icon: XCircle,
  },
  cancelled: {
    label: "Cancelled",
    className: "border-slate-700 bg-slate-950 text-slate-300",
    icon: XCircle,
  },
};

function sectionProgress(job: EngagementReportJobStatusResponse): number {
  if (job.total_sections <= 0) {
    return 0;
  }
  return Math.min(100, Math.round((job.completed_sections.length / job.total_sections) * 100));
}

function sectionSummary(job: EngagementReportJobStatusResponse): string {
  if (job.total_sections <= 0) {
    return `${job.completed_sections.length} completed`;
  }
  return `${job.completed_sections.length} of ${job.total_sections} completed`;
}

function statusLabel(job: EngagementReportJobStatusResponse): string {
  const completed = job.completed_sections.length;
  const total = job.total_sections;
  const progress = total > 0 ? `${completed} of ${total}` : `${completed} completed`;
  const isRetry = job.status === "queued" && job.attempt_count > 0;
  const nextAttempt = Math.min(job.max_attempts, job.attempt_count + 1);

  if (isRetry && job.generation_phase === "finalizing") {
    return `Retrying finalization · attempt ${nextAttempt} of ${job.max_attempts}`;
  }
  if (isRetry) {
    const sectionNumber = job.failure_details?.failed_section_order ?? completed + 1;
    return `Retrying section ${sectionNumber} · attempt ${nextAttempt} of ${job.max_attempts}`;
  }
  if (job.status === "generating" && job.generation_phase === "finalizing") {
    return `Finalizing · ${progress}`;
  }
  if (job.status === "generating") {
    return `Generating · ${progress}`;
  }
  return STATUS_CONTENT[job.status].label;
}

function failedSectionLabel(job: EngagementReportJobStatusResponse): string | null {
  const details = job.failure_details;
  const sectionId = details?.failed_section_id?.trim();
  if (!sectionId) {
    return null;
  }
  const sectionType = details?.failed_section_type?.trim();
  const sectionOrder = details?.failed_section_order;
  const prefix = typeof sectionOrder === "number" ? `Section ${sectionOrder}` : "Section";
  return sectionType ? `${prefix}: ${sectionId} (${sectionType})` : `${prefix}: ${sectionId}`;
}

function validationIssueLabels(job: EngagementReportJobStatusResponse): string[] {
  return (job.failure_details?.validation_issues ?? [])
    .map((issue) => {
      const code = issue.code.trim();
      const path = issue.path.trim();
      return code && path ? `${code} at ${path}` : null;
    })
    .filter((issue): issue is string => Boolean(issue))
    .slice(0, 3);
}

export function ReportJobProgress({
  job,
  isSubmitting = false,
  isLoading = false,
  isError = false,
  errorMessage = null,
}: ReportJobProgressProps) {
  const safeError = safeReportingMessage(
    errorMessage,
    "Report generation failed. Review selected inputs and try again.",
  );

  if (!job && !isSubmitting && !isLoading && !isError) {
    return null;
  }

  if (!job) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <h2 className="text-base font-semibold text-slate-100">Progress</h2>
          <p className="text-sm text-slate-400 sm:text-right" aria-live="polite">
            {isSubmitting
              ? "Submitting report generation request..."
              : isLoading
                ? "Loading report progress..."
                : isError
                  ? (safeError ?? "Could not load report progress.")
                  : "No active generation."}
          </p>
          {isSubmitting ? (
            <Badge
              variant="outline"
              className="h-6 w-fit max-w-full whitespace-nowrap border-cyan-700/70 bg-cyan-950/60 px-2 py-0 text-[11px] font-medium leading-none text-cyan-200"
            >
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              Submitting
            </Badge>
          ) : null}
        </div>
      </section>
    );
  }

  const statusContent = STATUS_CONTENT[job.status];
  const visibleStatusLabel = statusLabel(job);
  const StatusIcon = statusContent.icon;
  const progressValue = sectionProgress(job);
  const jobError = safeReportingMessage(
    job.error_message,
    "Report generation failed. Review selected inputs and try again.",
  );
  const failedSection = failedSectionLabel(job);
  const validationIssues = validationIssueLabels(job);
  const isActive = job.status === "queued" || job.status === "generating";

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-100">Progress</h2>
          <p className="mt-1 text-sm text-slate-400" aria-live="polite">
            {isActive
              ? "Report generation is in progress."
              : `Report generation is ${statusContent.label.toLowerCase()}.`}
          </p>
        </div>
        <Badge
          variant="outline"
          className={cn(
            "min-h-6 h-auto w-fit max-w-full whitespace-normal px-2 py-1 text-[11px] font-medium leading-tight",
            statusContent.className,
          )}
        >
          <StatusIcon
            className={cn("mr-1.5 h-3.5 w-3.5", job.status === "generating" && "animate-spin")}
          />
          {visibleStatusLabel}
        </Badge>
      </div>

      <div className="mt-3 grid gap-3">
        <div>
          <div className="mb-1 flex items-center justify-between gap-3 text-xs text-slate-300">
            <span>Sections</span>
            <span>{sectionSummary(job)}</span>
          </div>
          <Progress
            value={progressValue}
            aria-label="Report section progress"
            className="h-2 bg-slate-800"
          />
        </div>

        <dl className="grid gap-2 text-xs text-slate-300 sm:grid-cols-2">
          <div className="flex items-start justify-between gap-3 rounded-md border border-slate-800 bg-slate-950/70 px-3 py-2">
            <dt className="text-slate-400">Current section</dt>
            <dd className="text-right font-medium text-slate-100">
              {job.current_section_id ?? "None"}
            </dd>
          </div>
          <div className="flex items-start justify-between gap-3 rounded-md border border-slate-800 bg-slate-950/70 px-3 py-2">
            <dt className="text-slate-400">Attempt</dt>
            <dd className="text-right font-medium text-slate-100">
              {job.attempt_count} of {job.max_attempts}
            </dd>
          </div>
        </dl>
      </div>

      {job.status === "failed" ? (
        <div className="mt-4 rounded-md border border-rose-900/70 bg-rose-950/20 p-3 text-sm text-rose-100">
          <div className="flex items-center gap-2 font-medium">
            <AlertTriangle className="h-4 w-4" />
            Report generation failed.
          </div>
          <p className="mt-2 text-rose-100">
            {jobError ?? "The backend did not provide a failure reason."}
          </p>
          {failedSection ? (
            <p className="mt-2 text-xs text-rose-100">{failedSection}</p>
          ) : null}
          {validationIssues.length > 0 ? (
            <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-rose-100">
              {validationIssues.map((issue) => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          ) : null}
          <p className="mt-2 text-xs text-rose-200">
            Selected inputs remain selected. Review the issue, then choose Generate
            Report again.
          </p>
        </div>
      ) : null}
    </section>
  );
}
