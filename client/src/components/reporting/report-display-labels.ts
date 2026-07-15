/**
 * Presentation labels for engagement reporting report types and titles.
 *
 * Responsibility: keep backend report_type values stable while rendering
 * product-facing report names consistently across the reporting workspace.
 */

import type { ReportType } from "@/types/reporting";

export function reportTypeDisplayLabel(reportType: ReportType): string {
  void reportType;
  return "Engagement Report";
}

export function reportTitleDisplayLabel(
  reportType: ReportType,
  title: string,
): string {
  if (reportType !== "pentest") {
    return title;
  }
  return title
    .replace(/\bPenetration Test\b/gi, "Engagement")
    .replace(/\bPentest\b/gi, "Engagement");
}

export function reportMarkdownDisplayText(
  reportType: ReportType,
  markdown: string,
): string {
  if (reportType !== "pentest") {
    return markdown;
  }
  return markdown
    .replace(/^#\s+Penetration Test Report\b/im, "# Engagement Report")
    .replace(/^#\s+Pentest Report\b/im, "# Engagement Report");
}
