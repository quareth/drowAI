/* Shared engagement indicator schema for compact badges and status pills. */

export type EngagementIndicatorTone =
  | "neutral"
  | "severityCritical"
  | "severityHigh"
  | "severityMedium"
  | "severityLow"
  | "severityInfo";

export type EngagementIndicatorSize = "md" | "xs";

const ENGAGEMENT_INDICATOR_TONE_CLASS: Record<EngagementIndicatorTone, string> = {
  neutral:
    "border-slate-500/35 bg-slate-800/35 text-slate-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
  severityCritical:
    "border-red-500/45 bg-red-500/10 text-red-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
  severityHigh:
    "border-orange-500/45 bg-orange-500/10 text-orange-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
  severityMedium:
    "border-amber-500/45 bg-amber-500/10 text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
  severityLow:
    "border-cyan-500/45 bg-cyan-500/10 text-cyan-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
  severityInfo:
    "border-slate-500/35 bg-slate-800/35 text-slate-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
};

const ENGAGEMENT_INDICATOR_SIZE_CLASS: Record<EngagementIndicatorSize, string> = {
  md: "",
  xs: "px-1.5 py-0.5 text-[10px] font-medium leading-tight",
};

export function engagementIndicatorToneClass(tone: EngagementIndicatorTone = "neutral"): string {
  return ENGAGEMENT_INDICATOR_TONE_CLASS[tone];
}

export function engagementIndicatorSizeClass(size: EngagementIndicatorSize = "md"): string {
  return ENGAGEMENT_INDICATOR_SIZE_CLASS[size];
}
