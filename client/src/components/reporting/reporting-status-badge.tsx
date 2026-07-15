/**
 * Compact badge for engagement reporting input readiness.
 *
 * Responsibility: translate reporting input state values into product-facing
 * labels and compact dark-workspace styles for tables and dense rows.
 */

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ReportingInputState } from "@/types/reporting";

type ReportingStatusBadgeContent = {
  label: string;
  className: string;
};

const STATUS_BADGE_CONTENT: Record<
  ReportingInputState,
  ReportingStatusBadgeContent
> = {
  not_prepared: {
    label: "Not prepared",
    className: "border-slate-600 bg-slate-900/70 text-slate-200",
  },
  preparing: {
    label: "Preparing",
    className: "border-sky-700/70 bg-sky-950/60 text-sky-200",
  },
  ready: {
    label: "Ready",
    className: "border-emerald-700/70 bg-emerald-950/60 text-emerald-200",
  },
  failed: {
    label: "Failed",
    className: "border-rose-700/70 bg-rose-950/60 text-rose-200",
  },
  stale: {
    label: "Stale",
    className: "border-amber-700/70 bg-amber-950/60 text-amber-200",
  },
};

const UNAVAILABLE_BADGE_CONTENT: ReportingStatusBadgeContent = {
  label: "Unavailable",
  className: "border-slate-700 bg-slate-950 text-slate-400",
};

interface ReportingStatusBadgeProps {
  inputState: ReportingInputState | (string & {}) | null | undefined;
  className?: string;
}

function resolveBadgeContent(
  inputState: ReportingStatusBadgeProps["inputState"],
): ReportingStatusBadgeContent {
  if (typeof inputState !== "string") {
    return UNAVAILABLE_BADGE_CONTENT;
  }

  if (Object.prototype.hasOwnProperty.call(STATUS_BADGE_CONTENT, inputState)) {
    return STATUS_BADGE_CONTENT[inputState as ReportingInputState];
  }

  return UNAVAILABLE_BADGE_CONTENT;
}

export function ReportingStatusBadge({
  inputState,
  className,
}: ReportingStatusBadgeProps) {
  if (inputState === "ready") {
    return null;
  }

  const content = resolveBadgeContent(inputState);

  return (
    <Badge
      variant="outline"
      aria-label={`Reporting input status: ${content.label}`}
      className={cn(
        "h-5 max-w-full whitespace-nowrap px-2 py-0 text-[11px] font-medium leading-none",
        content.className,
        className,
      )}
    >
      {content.label}
    </Badge>
  );
}
