/**
 * Single task card with status actions, overflow menu, and optional container monitor block.
 * Extracted from TaskPanel to keep the panel file within size limits.
 */

import { DockerTerminal } from "@/components/docker-terminal";
import { ResourcesPanel } from "@/components/resources-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Task } from "@/types";
import type { ReportingInputState } from "@/types/reporting";
import { cn } from "@/lib/utils";
import {
  Container,
  FileText,
  MoreVertical,
  Pause,
  Play,
  RefreshCw,
  Square,
  Terminal,
  Trash2,
} from "lucide-react";

function getStatusColor(status: string) {
  switch (status) {
    case "running":
      return "bg-green-600";
    case "paused":
      return "bg-yellow-600";
    case "completed":
      return "bg-gray-600";
    case "failed":
      return "bg-red-600";
    default:
      return "bg-blue-600";
  }
}

function getStatusText(status: string) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function getReportingInputLabel(
  inputState: ReportingInputState | undefined,
  explicitLabel: string | undefined,
) {
  if (explicitLabel?.trim()) {
    return explicitLabel.trim();
  }

  switch (inputState) {
    case "not_prepared":
      return "Not prepared";
    case "preparing":
      return "Preparing";
    case "ready":
      return "Ready";
    case "failed":
      return "Failed";
    case "stale":
      return "Stale";
    default:
      return null;
  }
}

const STOPPABLE_STATUSES = new Set(["queued", "starting", "running", "paused", "pausing", "resuming"]);
const STARTABLE_STATUSES = new Set(["created", "stopped", "failed", "timeout"]);

export interface TaskPanelCardProps {
  task: Task;
  showEngagementLabel?: boolean;
  selectedTask: number | null;
  showContainerMonitor: boolean;
  taskActionPending: boolean;
  isRefreshing: boolean;
  canTaskControl?: boolean;
  canTaskDelete?: boolean;
  reportingInputState?: ReportingInputState;
  reportingInputLabel?: string;
  canPrepareReportingInput?: boolean;
  isPreparingReportingInput?: boolean;
  isTaskDeleting: (taskId: number) => boolean;
  onToggleMonitor: (taskId: number) => void;
  onRefresh: () => void;
  onViewDetails: (taskId: number, taskName: string) => void;
  onDelete: (taskId: number, taskName: string) => void;
  onTaskAction: (taskId: number, action: string) => void;
  onOpenTerminal: (taskId: number) => void;
  onPrepareReportingInput?: (taskId: number) => void;
  onOpenReportsWorkspace?: (engagementId: number | null | undefined) => void;
}

export function TaskPanelCard({
  task,
  showEngagementLabel,
  selectedTask,
  showContainerMonitor,
  taskActionPending,
  isRefreshing,
  canTaskControl = true,
  canTaskDelete = true,
  reportingInputState,
  reportingInputLabel,
  canPrepareReportingInput = false,
  isPreparingReportingInput = false,
  isTaskDeleting,
  onToggleMonitor,
  onRefresh,
  onViewDetails,
  onDelete,
  onTaskAction,
  onOpenTerminal,
  onPrepareReportingInput,
  onOpenReportsWorkspace,
}: TaskPanelCardProps) {
  const reportingLabel = getReportingInputLabel(reportingInputState, reportingInputLabel);
  const hasReportsWorkspaceAction = typeof onOpenReportsWorkspace === "function";
  const canOpenReportsWorkspace = task.engagement_id != null;
  const reportsWorkspaceButton = hasReportsWorkspaceAction ? (
    <Button
      size="sm"
      variant="secondary"
      onClick={() => onOpenReportsWorkspace(task.engagement_id)}
      className="h-7 rounded-md bg-slate-600 px-2 text-[11px] text-white hover:bg-slate-500"
      disabled={!canOpenReportsWorkspace}
    >
      Reports Workspace
    </Button>
  ) : null;

  return (
    <Card
      data-testid={`task-card-${task.id}`}
      className="rounded-md border-slate-700/60 bg-slate-800/40 shadow-sm backdrop-blur-sm transition-all hover:border-slate-600/60 hover:bg-slate-800/60"
    >
      <CardContent className="p-2.5">
        <div className="mb-1.5 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="mb-0.5 truncate text-sm font-semibold leading-tight text-slate-100">{task.name}</h3>
            <p className="truncate text-[11px] leading-4 text-slate-400">
              {task.scope?.split("\n")[0] || "No scope defined"}
            </p>
            {showEngagementLabel && task.engagement_name ? (
              <p className="truncate text-[10px] leading-4 text-slate-500">{task.engagement_name}</p>
            ) : null}
          </div>
          <div className="flex items-center gap-1.5">
            <Badge
              className={cn(
                "status-indicator h-5 pl-3 pr-2 text-[10px] font-medium tracking-wide",
                `status-${task.status}`,
                getStatusColor(task.status),
              )}
            >
              {getStatusText(task.status)}
            </Badge>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Task actions for ${task.name}`}
                  className="h-7 w-7 p-0 text-slate-400 hover:bg-slate-700/60 hover:text-slate-100"
                >
                  <MoreVertical className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[10rem]">
                {canTaskControl ? (
                  <DropdownMenuItem className="text-xs" onClick={() => onToggleMonitor(task.id)}>
                    <Container className="mr-2 h-4 w-4" />
                    {selectedTask === task.id && showContainerMonitor ? "Hide Monitor" : "Container Status"}
                  </DropdownMenuItem>
                ) : null}
                <DropdownMenuItem className="text-xs" onClick={onRefresh} disabled={isRefreshing}>
                  <RefreshCw className="mr-2 h-4 w-4" />
                  {isRefreshing ? "Refreshing..." : "Refresh"}
                </DropdownMenuItem>
                <DropdownMenuItem className="text-xs" onClick={() => onViewDetails(task.id, task.name)}>
                  <FileText className="mr-2 h-4 w-4" />
                  View Details
                </DropdownMenuItem>
                {canTaskDelete ? (
                  <DropdownMenuItem
                    className="text-xs text-red-400 focus:text-red-300"
                    onClick={() => onDelete(task.id, task.name)}
                    disabled={isTaskDeleting(task.id)}
                  >
                    <Trash2 className="mr-2 h-4 w-4" />
                    {isTaskDeleting(task.id) ? "Deleting..." : "Delete Task"}
                  </DropdownMenuItem>
                ) : null}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

        <div className="mb-2 text-[11px] text-slate-400">
          <span>Not started</span>
          {reportingLabel ? (
            <div className="mt-0.5 flex items-center gap-1.5 text-slate-500">
              <span>Report input:</span>
              <span className="font-medium text-slate-300">{reportingLabel}</span>
            </div>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {canTaskControl && STOPPABLE_STATUSES.has(task.status) && (
            <Button
              size="sm"
              onClick={() => onTaskAction(task.id, "stop")}
              className="h-7 rounded-md bg-red-600 px-2 text-[11px] text-white hover:bg-red-500 [&_svg]:size-3"
              disabled={taskActionPending}
            >
              <Square className="h-3 w-3" />
              Stop
            </Button>
          )}

          {canTaskControl && task.status === "running" && (
            <Button
              size="sm"
              onClick={() => onTaskAction(task.id, "pause")}
              className="h-7 rounded-md bg-orange-600 px-2 text-[11px] text-white hover:bg-orange-500 [&_svg]:size-3"
              disabled={taskActionPending}
            >
              <Pause className="h-3 w-3" />
              Pause
            </Button>
          )}

          {canTaskControl && task.status === "paused" && (
            <Button
              size="sm"
              onClick={() => onTaskAction(task.id, "resume")}
              className="h-7 rounded-md bg-green-600 px-2 text-[11px] text-white hover:bg-green-500 [&_svg]:size-3"
              disabled={taskActionPending}
            >
              <Play className="h-3 w-3" />
              Resume
            </Button>
          )}

          {canTaskControl && task.status === "pausing" && (
            <Button size="sm" variant="secondary" disabled className="h-7 rounded-md px-2 text-[11px] [&_svg]:size-3">
              <Pause className="h-3 w-3" />
              Pausing...
            </Button>
          )}

          {canTaskControl && task.status === "resuming" && (
            <Button size="sm" variant="secondary" disabled className="h-7 rounded-md px-2 text-[11px] [&_svg]:size-3">
              <Play className="h-3 w-3" />
              Resuming...
            </Button>
          )}

          {canTaskControl && STARTABLE_STATUSES.has(task.status) && (
            <Button
              size="sm"
              onClick={() => onTaskAction(task.id, "start")}
              className="h-7 rounded-md bg-green-600 px-2 text-[11px] text-white hover:bg-green-500 [&_svg]:size-3"
              disabled={taskActionPending}
            >
              <Play className="h-3 w-3" />
              Start
            </Button>
          )}

          {canTaskControl && (task.status === "running" ||
            task.status === "paused" ||
            task.status === "pausing" ||
            task.status === "resuming") && (
            <>
              <Button
                size="sm"
                variant="secondary"
                className="h-7 rounded-md bg-slate-600 px-2 text-[11px] text-white hover:bg-slate-500 [&_svg]:size-3"
                onClick={() => onOpenTerminal(task.id)}
              >
                <Terminal className="h-3 w-3" />
                Shell
              </Button>
            </>
          )}

          {canPrepareReportingInput && onPrepareReportingInput ? (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => onPrepareReportingInput(task.id)}
              className="h-7 rounded-md border border-slate-600 bg-slate-700/60 px-2 text-[11px] text-slate-100 hover:bg-slate-600 [&_svg]:size-3"
              disabled={isPreparingReportingInput}
            >
              {isPreparingReportingInput ? "Preparing..." : "Prepare"}
            </Button>
          ) : null}

          {task.status !== "completed" ? reportsWorkspaceButton : null}

          {canTaskControl && task.status === "completed" && (
            hasReportsWorkspaceAction ? (
              reportsWorkspaceButton
            ) : (
              <Button size="sm" className="h-7 rounded-md bg-blue-600 px-2 text-[11px] text-white hover:bg-blue-500">
                Report
              </Button>
            )
          )}
        </div>
      </CardContent>

      {canTaskControl && selectedTask === task.id && showContainerMonitor && (
        <div className="border-t border-slate-700 p-4">
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
            <div className="xl:col-span-3">
              <DockerTerminal taskId={task.id} canTaskControl={canTaskControl} />
            </div>
            <ResourcesPanel taskId={task.id.toString()} className="xl:col-span-2" />
          </div>
        </div>
      )}
    </Card>
  );
}
