/**
 * Current report preview panel.
 *
 * Responsibility: render an already-loaded engagement report safely without
 * fetching source material or owning report selection state.
 */

import { AlertCircle, Download, FileText, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  reportMarkdownDisplayText,
  reportTitleDisplayLabel,
} from "@/components/reporting/report-display-labels";
import { downloadTextFile } from "@/lib/browser-download";
import { cn } from "@/lib/utils";
import type {
  EngagementReportReadResponse,
  EngagementReportSection,
  EngagementReportSectionBlock,
  ReportStatus,
} from "@/types/reporting";

interface ReportPreviewProps {
  report: EngagementReportReadResponse | null;
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string | null;
  emptyMessage?: string;
  isDeleting?: boolean;
  onDeleteReport?: (report: EngagementReportReadResponse) => void;
}

const STATUS_LABELS: Record<ReportStatus, string> = {
  generating: "Generating",
  ready: "Ready",
  failed: "Failed",
};

const STATUS_STYLES: Record<ReportStatus, string> = {
  generating: "border-cyan-700/70 bg-cyan-950/60 text-cyan-200",
  ready: "border-emerald-700/70 bg-emerald-950/60 text-emerald-200",
  failed: "border-rose-700/70 bg-rose-950/60 text-rose-200",
};
const DEFAULT_PREVIEW_TITLE = "Engagement Report Preview";

function previewTitleLabel(title: string): string {
  return `${title} Preview`;
}

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

function sourceCountLabel(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function nonEmpty(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function blockText(block: EngagementReportSectionBlock): string[] {
  return [
    `### ${block.title}`,
    nonEmpty(block.content_markdown),
    nonEmpty(block.impact_markdown) ? `Impact\n${block.impact_markdown}` : null,
    nonEmpty(block.remediation_markdown)
      ? `Remediation\n${block.remediation_markdown}`
      : null,
  ].filter((value): value is string => value !== null);
}

function sectionText(section: EngagementReportSection): string | null {
  const parts = [
    `## ${section.title}`,
    nonEmpty(section.content_markdown),
    ...section.blocks.flatMap(blockText),
    section.unsupported_notes.length > 0
      ? `Unsupported notes\n${section.unsupported_notes.join("\n")}`
      : null,
    section.generation_notes.length > 0
      ? `Generation notes\n${section.generation_notes.join("\n")}`
      : null,
  ].filter((value): value is string => value !== null);

  return parts.length > 1 ? parts.join("\n\n") : null;
}

function reportBodyText(report: EngagementReportReadResponse): string | null {
  const snapshot = nonEmpty(report.markdown_snapshot);
  if (snapshot) {
    return reportMarkdownDisplayText(report.report_type, snapshot);
  }

  const sections = report.sections
    .map(sectionText)
    .filter((value): value is string => value !== null);
  return sections.length > 0
    ? reportMarkdownDisplayText(report.report_type, sections.join("\n\n"))
    : null;
}

function reportDownloadFilename(report: EngagementReportReadResponse): string {
  const safeTitle = reportTitleDisplayLabel(report.report_type, report.title)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  const baseName = safeTitle || `${report.report_type}-report`;
  return `${baseName}-v${report.version}.md`;
}

export function ReportPreview({
  report,
  isLoading = false,
  isError = false,
  errorMessage = null,
  emptyMessage = "No current report for this type.",
  isDeleting = false,
  onDeleteReport,
}: ReportPreviewProps) {
  if (isLoading) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
        <h2 className="text-base font-semibold text-slate-100">
          {DEFAULT_PREVIEW_TITLE}
        </h2>
        <p className="mt-2 text-sm text-slate-400" aria-live="polite">
          Loading current report...
        </p>
      </section>
    );
  }

  if (isError) {
    return (
      <section className="rounded-lg border border-rose-900/70 bg-rose-950/20 p-4">
        <div className="flex items-center gap-2 text-base font-semibold text-rose-100">
          <AlertCircle className="h-4 w-4" />
          {DEFAULT_PREVIEW_TITLE}
        </div>
        <p className="mt-2 text-sm text-rose-100" aria-live="polite">
          {errorMessage ?? "Could not load current report."}
        </p>
      </section>
    );
  }

  if (!report) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
        <div className="flex items-center gap-2 text-base font-semibold text-slate-100">
          <FileText className="h-4 w-4" />
          {DEFAULT_PREVIEW_TITLE}
        </div>
        <p className="mt-2 text-sm text-slate-400" aria-live="polite">
          {emptyMessage}
        </p>
      </section>
    );
  }

  const bodyText = reportBodyText(report);
  const displayTitle = reportTitleDisplayLabel(report.report_type, report.title);
  const previewTitle = previewTitleLabel(displayTitle);
  const evidenceCount = report.source_evidence_refs.length;
  const knowledgeCount = report.source_knowledge_refs.length;
  const metadataItems = [
    formatTimestamp(report.generated_at),
    sourceCountLabel(report.source_task_memo_ids.length, "task", "tasks"),
    sourceCountLabel(evidenceCount, "evidence", "evidence"),
    sourceCountLabel(knowledgeCount, "knowledge", "knowledge"),
  ];
  const showStatusBadge = report.status !== "ready";
  const handleDownload = () => {
    if (!bodyText) {
      return;
    }
    downloadTextFile(
      bodyText,
      reportDownloadFilename(report),
      "text/markdown;charset=utf-8",
    );
  };

  return (
    <section className="flex min-h-[36rem] flex-col rounded-lg border border-slate-800 bg-slate-900/70 p-4 shadow-2xl shadow-slate-950/30">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="break-words text-lg font-semibold text-white">
            {previewTitle}
          </h2>
          <p className="mt-1 text-xs text-slate-400">Version {report.version}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {bodyText ? (
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="h-8 w-8 border-slate-800 bg-transparent text-slate-300 hover:border-slate-700 hover:bg-slate-800/70 hover:text-white"
              onClick={handleDownload}
              aria-label="Download report"
              title="Download report"
            >
              <Download className="h-3.5 w-3.5" aria-hidden="true" />
            </Button>
          ) : null}
          {onDeleteReport ? (
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="h-8 w-8 border-slate-800 bg-transparent text-slate-300 hover:border-rose-800 hover:bg-rose-950/40 hover:text-rose-100"
              onClick={() => onDeleteReport(report)}
              disabled={isDeleting}
              aria-label="Delete report"
              title="Delete report"
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            </Button>
          ) : null}
          {showStatusBadge ? (
            <Badge
              variant="outline"
              className={cn(
                "h-6 w-fit max-w-full whitespace-nowrap px-2 py-0 text-[11px] font-medium leading-none",
                STATUS_STYLES[report.status],
              )}
            >
              {STATUS_LABELS[report.status]}
            </Badge>
          ) : null}
        </div>
      </div>

      <div className="mt-2 text-xs text-slate-400">
        {metadataItems.map((item, index) => (
          <span key={`${item}-${index}`}>
            {index > 0 ? <span className="px-1.5 text-slate-600">·</span> : null}
            {item}
          </span>
        ))}
      </div>

      <div className="mt-4 min-h-0 overflow-hidden rounded-md border border-slate-800 bg-slate-950/80 shadow-inner shadow-slate-950/40">
        {bodyText ? (
          <article
            className={cn(
              "max-h-[34rem] min-h-[26rem] overflow-y-auto overscroll-contain px-6 py-7 md:px-8 lg:max-h-[calc(100vh-18rem)]",
              "prose prose-sm prose-invert max-w-none leading-7 md:prose-base",
              "prose-headings:scroll-mt-4 prose-headings:font-semibold prose-headings:text-slate-50",
              "prose-h1:mb-6 prose-h1:border-b prose-h1:border-slate-800 prose-h1:pb-4 prose-h1:text-2xl",
              "prose-h2:mb-4 prose-h2:mt-9 prose-h2:border-b prose-h2:border-slate-800/80 prose-h2:pb-2 prose-h2:text-xl",
              "prose-h3:mb-2 prose-h3:mt-7 prose-h3:text-lg",
              "prose-p:text-slate-200 prose-p:leading-8 prose-strong:text-slate-50",
              "prose-a:text-cyan-300 prose-a:no-underline hover:prose-a:text-cyan-200",
              "prose-code:rounded prose-code:bg-slate-900 prose-code:px-1 prose-code:py-0.5 prose-code:text-[12px] prose-code:text-emerald-300 prose-code:before:content-none prose-code:after:content-none",
              "prose-pre:overflow-x-auto prose-pre:rounded-md prose-pre:border prose-pre:border-slate-800 prose-pre:bg-slate-900 prose-pre:text-slate-200",
              "prose-blockquote:border-l-cyan-700 prose-blockquote:text-slate-300",
              "prose-li:my-1 prose-li:marker:text-slate-500 prose-table:text-slate-200 prose-th:border-slate-700 prose-td:border-slate-800",
            )}
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{bodyText}</ReactMarkdown>
          </article>
        ) : (
          <p className="p-4 text-sm text-slate-400">
            This report does not include previewable content yet.
          </p>
        )}
      </div>
    </section>
  );
}
