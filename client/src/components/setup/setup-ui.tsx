/**
 * Shared presentation primitives for the first-run setup wizard.
 */
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

interface SetupStepHeaderProps {
  icon: LucideIcon;
  title: string;
  description: string;
}

export function SetupStepHeader({ icon: Icon, title, description }: SetupStepHeaderProps) {
  return (
    <div className="space-y-3">
      <div className="flex h-10 w-10 items-center justify-center rounded-md border border-slate-700 bg-slate-900 text-slate-200">
        <Icon className="h-5 w-5" />
      </div>
      <div className="space-y-1">
        <h2 className="text-xl font-semibold text-slate-50">{title}</h2>
        <p className="max-w-2xl text-sm leading-6 text-slate-400">{description}</p>
      </div>
    </div>
  );
}

interface SetupCalloutProps {
  children: ReactNode;
  className?: string;
}

export function SetupCallout({ children, className }: SetupCalloutProps) {
  return (
    <div className={cn("rounded-md border border-slate-800 bg-slate-900/45 p-4 text-sm text-slate-300", className)}>
      {children}
    </div>
  );
}

interface SetupActionsProps {
  children: ReactNode;
  className?: string;
}

export function SetupActions({ children, className }: SetupActionsProps) {
  return (
    <div className={cn("flex flex-col-reverse gap-3 border-t border-slate-800 pt-5 sm:flex-row sm:justify-between", className)}>
      {children}
    </div>
  );
}

interface ReviewRowProps {
  icon: LucideIcon;
  title: string;
  children: ReactNode;
}

export function ReviewRow({ icon: Icon, title, children }: ReviewRowProps) {
  return (
    <div className="flex gap-3 rounded-md border border-slate-800 bg-slate-950/40 p-4">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-slate-700 bg-slate-900 text-slate-300">
        <Icon className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1">
        <h3 className="text-sm font-medium text-slate-100">{title}</h3>
        <div className="mt-1 space-y-1 text-sm text-slate-400">{children}</div>
      </div>
    </div>
  );
}
