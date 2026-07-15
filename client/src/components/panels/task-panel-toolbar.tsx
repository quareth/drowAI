/**
 * Compact TaskPanel header: view mode toggle, filter placeholder, split create menu.
 */

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Filter, LayoutList, List, ListTodo, Plus, Search, X } from "lucide-react";

export interface TaskPanelToolbarProps {
  viewMode: "grouped" | "flat";
  onViewMode: (mode: "grouped" | "flat") => void;
  onNewTask: () => void;
  onNewEngagement: () => void;
  nameFilter: string;
  onNameFilterChange: (value: string) => void;
  canCreateTask?: boolean;
  canCreateEngagement?: boolean;
}

export function TaskPanelToolbar({
  viewMode,
  onViewMode,
  onNewTask,
  onNewEngagement,
  nameFilter,
  onNameFilterChange,
  canCreateTask = true,
  canCreateEngagement = true,
}: TaskPanelToolbarProps) {
  const canOpenCreateMenu = canCreateTask || canCreateEngagement;
  const hasNameFilter = nameFilter.trim().length > 0;

  return (
    <div className="flex shrink-0 items-center justify-between border-b border-slate-800/30 bg-slate-900/30 px-3 py-1.5">
      <div className="flex items-center space-x-2">
        <ListTodo className="h-3 w-3 text-emerald-400" />
        <span className="text-xs font-medium text-slate-200">Operations</span>
      </div>
      <div className="flex items-center space-x-1">
        <Button
          variant={viewMode === "grouped" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 px-2 text-slate-400"
          onClick={() => onViewMode("grouped")}
          title="Grouped view"
        >
          <LayoutList className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant={viewMode === "flat" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 px-2 text-slate-400"
          onClick={() => onViewMode("flat")}
          title="Flat view"
        >
          <List className="h-3.5 w-3.5" />
        </Button>
        <Popover>
          <PopoverTrigger asChild>
            <Button
              variant={hasNameFilter ? "secondary" : "ghost"}
              size="sm"
              className="rounded p-1 text-slate-400 transition-colors hover:bg-slate-800/30 hover:text-slate-200"
              aria-label="Filter tasks and engagements"
              title="Filter by name"
            >
              <Filter className="h-3 w-3" />
            </Button>
          </PopoverTrigger>
          <PopoverContent
            align="end"
            className="w-64 border-slate-700 bg-slate-900 p-2 text-slate-100"
          >
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
              <Input
                type="text"
                value={nameFilter}
                onChange={(event) => onNameFilterChange(event.target.value)}
                placeholder="Task or engagement name"
                aria-label="Task or engagement name filter"
                autoFocus
                className="h-8 border-slate-700 bg-slate-950 pl-7 pr-8 text-xs text-slate-100 placeholder:text-slate-500 focus-visible:ring-emerald-500"
              />
              {hasNameFilter ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="absolute right-1 top-1/2 h-6 w-6 -translate-y-1/2 p-0 text-slate-500 hover:bg-slate-800 hover:text-slate-200"
                  onClick={() => onNameFilterChange("")}
                  aria-label="Clear name filter"
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              ) : null}
            </div>
          </PopoverContent>
        </Popover>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              size="sm"
              className="h-7 rounded-md bg-emerald-600/90 px-2 text-[11px] font-semibold text-white hover:bg-emerald-500"
              disabled={!canOpenCreateMenu}
              aria-disabled={!canOpenCreateMenu}
            >
              <Plus className="mr-1 h-3 w-3" />
              New
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="border-slate-700 bg-slate-900 text-slate-100">
            <DropdownMenuItem
              className="text-xs focus:bg-slate-800"
              onClick={onNewTask}
              disabled={!canCreateTask}
            >
              New Task
            </DropdownMenuItem>
            <DropdownMenuItem
              className="text-xs focus:bg-slate-800"
              onClick={onNewEngagement}
              disabled={!canCreateEngagement}
            >
              New Engagement
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
