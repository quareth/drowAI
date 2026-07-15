/* Shared visual tokens for engagement workspace surfaces in the refined neo-dark UI refresh. */

export const engagementShellPanelClass =
  "rounded-xl border border-slate-700/70 bg-slate-900/70 shadow-[0_10px_35px_-18px_rgba(15,23,42,0.95)] backdrop-blur-sm";

export const engagementShellPanelMutedClass =
  "rounded-xl border border-slate-800/80 bg-slate-950/70 shadow-[inset_0_1px_0_rgba(148,163,184,0.06)]";

export const engagementCardClass = `${engagementShellPanelClass} transition-colors duration-150`;

export const engagementInsetClass =
  "rounded-lg border border-slate-800/90 bg-slate-950/80 shadow-[inset_0_1px_0_rgba(148,163,184,0.05)]";

export const engagementFilterBarClass =
  "grid gap-2 rounded-t-xl border-b border-slate-800/90 bg-slate-950/75 p-3 backdrop-blur-sm";

export const engagementInputClass =
  "h-9 border-slate-700/80 bg-slate-950/90 text-xs text-slate-100 placeholder:text-slate-500 transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-emerald-400/70 focus-visible:ring-offset-0";

/** @deprecated Use Radix Select with engagementSelectTriggerClass instead. */
export const engagementSelectClass =
  "h-9 rounded-md border border-slate-700/80 bg-slate-950/90 px-2 text-xs text-slate-100 transition-colors duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-emerald-400/70";

export const engagementSelectTriggerClass =
  "h-9 text-xs focus:ring-emerald-400/70 focus:ring-offset-0";

export const engagementTableHeadClass =
  "sticky top-0 z-10 bg-slate-950/95 backdrop-blur supports-[backdrop-filter]:bg-slate-950/85";

export const engagementRowClass =
  "cursor-pointer border-slate-800/90 transition-colors duration-150 odd:bg-slate-900/20 hover:bg-slate-800/45";

export const engagementRowSelectedClass =
  "bg-emerald-900/18 ring-1 ring-inset ring-emerald-600/40";

export const engagementInlineButtonClass =
  "inline-flex items-center rounded-full border border-slate-600/80 bg-slate-900/60 px-2 py-0.5 text-[11px] text-slate-200 transition-colors duration-150 hover:bg-slate-800/80 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/70";

export const engagementDetailSectionClass = `${engagementInsetClass} space-y-1 p-3`;
