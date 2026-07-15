/**
 * Collapsible engagement header with summary and overflow actions for TaskPanel grouped view.
 */

import type { ReactNode } from "react";
import { Archive, BookOpen, ChevronDown, ChevronRight, MoreVertical, Plus, RotateCcw } from "lucide-react";
import { useLocation } from "wouter";

import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { EngagementGroup } from "@/lib/task-grouping";
import type { Task } from "@/types";

export interface EngagementSectionProps {
  group: EngagementGroup;
  isExpanded: boolean;
  isEngagementMutationPending?: boolean;
  canCreateTask?: boolean;
  canMutateEngagement?: boolean;
  onToggleExpand: () => void;
  onAddTask: (engagementId: number) => void;
  onArchiveEngagement: (engagementId: number) => void;
  onRestoreEngagement: (engagementId: number) => void;
  renderTaskCard: (task: Task) => ReactNode;
}

export function EngagementSection({
  group,
  isExpanded,
  isEngagementMutationPending = false,
  canCreateTask = true,
  canMutateEngagement = true,
  onToggleExpand,
  onAddTask,
  onArchiveEngagement,
  onRestoreEngagement,
  renderTaskCard,
}: EngagementSectionProps) {
  const [, setLocation] = useLocation();

  return (
    <Collapsible
      open={isExpanded}
      onOpenChange={(next) => {
        if (next !== isExpanded) {
          onToggleExpand();
        }
      }}
      className="rounded-md border border-slate-800/80 bg-slate-900/40"
    >
      <div className="flex items-start gap-1 px-2 py-1.5">
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="h-8 shrink-0 px-1 text-slate-300 hover:bg-slate-800/60 hover:text-white">
            {isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </Button>
        </CollapsibleTrigger>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <CollapsibleTrigger asChild>
              <button
                type="button"
                className="truncate text-left text-sm font-medium text-slate-100 hover:text-white"
              >
                {group.engagementName}
              </button>
            </CollapsibleTrigger>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Engagement actions for ${group.engagementName}`}
                  className="h-7 w-7 shrink-0 p-0 text-slate-400 hover:bg-slate-800/60"
                >
                  <MoreVertical className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[10rem] border-slate-700 bg-slate-900 text-slate-100">
                {group.engagementId != null ? (
                  <DropdownMenuItem
                    className="text-xs focus:bg-slate-800"
                    onClick={() => onAddTask(group.engagementId!)}
                    disabled={!canCreateTask}
                  >
                    <Plus className="mr-2 h-4 w-4" />
                    Add Task
                  </DropdownMenuItem>
                ) : null}
                <DropdownMenuItem
                  className="text-xs focus:bg-slate-800"
                  onClick={() => setLocation("/knowledge")}
                >
                  <BookOpen className="mr-2 h-4 w-4" />
                  View Knowledge
                </DropdownMenuItem>
                {group.engagementId != null && group.engagementStatus != null && canMutateEngagement ? (
                  group.engagementStatus === "archived" ? (
                    <DropdownMenuItem
                      className="text-xs focus:bg-slate-800"
                      disabled={isEngagementMutationPending}
                      onClick={() => onRestoreEngagement(group.engagementId!)}
                    >
                      <RotateCcw className="mr-2 h-4 w-4" />
                      Restore Engagement
                    </DropdownMenuItem>
                  ) : (
                    <DropdownMenuItem
                      className="text-xs text-amber-300 focus:bg-slate-800 focus:text-amber-200"
                      disabled={isEngagementMutationPending}
                      onClick={() => onArchiveEngagement(group.engagementId!)}
                    >
                      <Archive className="mr-2 h-4 w-4" />
                      Archive Engagement
                    </DropdownMenuItem>
                  )
                ) : null}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          <p className="pl-0 text-[11px] text-slate-500">{group.statusSummary}</p>
        </div>
      </div>
      <CollapsibleContent className="space-y-2 px-2 pb-2 pt-0">
        {group.tasks.map((t) => renderTaskCard(t))}
      </CollapsibleContent>
    </Collapsible>
  );
}
