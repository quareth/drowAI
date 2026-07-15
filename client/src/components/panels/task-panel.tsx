/**
 * Task list panel for Overview: flat or grouped-by-engagement views, task actions, and create flows.
 */

import { useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";
import { Task } from "@/types";
import { Button } from "@/components/ui/button";
import { NewTaskModal } from "@/components/modals/new-task-modal";
import { NewEngagementModal } from "@/components/modals/new-engagement-modal";
import { ScopeDetailsModal } from "@/components/modals/scope-details-modal";
import { EngagementSection } from "@/components/panels/engagement-section";
import { TaskPanelCard } from "@/components/panels/task-panel-card";
import { TaskPanelToolbar } from "@/components/panels/task-panel-toolbar";
import { queryClient } from "@/lib/queryClient";
import { filterTasksByName, normalizeTaskPanelNameFilter } from "@/lib/task-grouping";
import { taskAdmissionErrorPresentation } from "@/lib/task-admission-errors";
import { TENANT_ACTIONS, hasTenantAction, toTenantActionSet } from "@/lib/tenant-permissions";
import { useToast } from "@/hooks/use-toast";
import { useTaskManagement } from "@/hooks/useTaskManagement";
import { useTenantContext } from "@/hooks/use-tenant-context";
import {
  canPrepareReportingInput,
  shouldRegeneratePreparedMemo,
  usePrepareTaskMemo,
  useTaskPanelReportingStatusProjection,
} from "@/hooks/use-reporting";
import { openTerminalForTask } from "@/state/workbench-state-store";
import { usePlanContext } from "@/contexts/PlanContext";
import { useArchiveEngagement, useRestoreEngagement } from "@/hooks/use-engagement-knowledge";
import {
  useTaskPanelEngagementGroups,
  useTaskPanelMutations,
  useTaskPanelViewState,
} from "@/hooks/use-task-panel";
import { ListTodo } from "lucide-react";

const ARCHIVE_BLOCKING_TASK_STATUSES = new Set([
  "queued",
  "starting",
  "running",
  "paused",
  "pausing",
  "resuming",
  "stopping",
  "waiting_for_human",
]);

export interface TaskPanelProps {
  searchQuery?: string;
  statusFilter?: string;
}

export function TaskPanel({ searchQuery = "", statusFilter = "all" }: TaskPanelProps) {
  const [nameFilter, setNameFilter] = useState(searchQuery);
  const [showNewTaskModal, setShowNewTaskModal] = useState(false);
  const [showNewEngagementModal, setShowNewEngagementModal] = useState(false);
  const [preselectedEngagementId, setPreselectedEngagementId] = useState<number | null>(null);
  const [selectedTask, setSelectedTask] = useState<number | null>(null);
  const [showContainerMonitor, setShowContainerMonitor] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [deletePhase, setDeletePhase] = useState("");
  const [deletingTasks, setDeletingTasks] = useState<Set<number>>(new Set());
  const [scopeDetailsModal, setScopeDetailsModal] = useState<{
    isOpen: boolean;
    taskId: number | null;
    taskName: string;
  }>({ isOpen: false, taskId: null, taskName: "" });

  const { toast } = useToast();
  const { clearState: clearPlanState } = usePlanContext();
  const [, setLocation] = useLocation();
  const { tasks, isLoading } = useTaskManagement();
  const { effectivePermissions } = useTenantContext();
  const archiveEngagementMutation = useArchiveEngagement();
  const restoreEngagementMutation = useRestoreEngagement();
  const prepareTaskMemoMutation = usePrepareTaskMemo();
  const tenantActionSet = useMemo(
    () => toTenantActionSet(effectivePermissions),
    [effectivePermissions],
  );
  const canCreateTask = hasTenantAction(tenantActionSet, TENANT_ACTIONS.taskCreate);
  const canControlTask = hasTenantAction(tenantActionSet, TENANT_ACTIONS.taskControl);
  const canDeleteTask = hasTenantAction(tenantActionSet, TENANT_ACTIONS.taskDelete);
  const canWriteKnowledge = hasTenantAction(tenantActionSet, TENANT_ACTIONS.knowledgeWrite);

  useEffect(() => {
    setNameFilter(searchQuery);
  }, [searchQuery]);

  useEffect(() => {
    if (!canCreateTask && showNewTaskModal) {
      setShowNewTaskModal(false);
      setPreselectedEngagementId(null);
    }
  }, [canCreateTask, showNewTaskModal]);

  useEffect(() => {
    if (!canWriteKnowledge && showNewEngagementModal) {
      setShowNewEngagementModal(false);
    }
  }, [canWriteKnowledge, showNewEngagementModal]);

  const filteredTasks = useMemo(
    () =>
      filterTasksByName(tasks, nameFilter).filter(
        (task: Task) => statusFilter === "all" || task.status === statusFilter,
      ),
    [nameFilter, statusFilter, tasks],
  );
  const {
    viewMode,
    setViewMode,
    expandedEngagements,
    toggleEngagementExpanded,
    showArchivedEngagements,
    setShowArchivedEngagements,
  } = useTaskPanelViewState(filteredTasks);
  const { engagementGroups } = useTaskPanelEngagementGroups({
    filteredTasks,
    searchQuery: nameFilter,
    showArchivedEngagements,
  });
  const hasNameFilter = normalizeTaskPanelNameFilter(nameFilter).length > 0;
  const reportingInventoryEngagementId = useMemo(() => {
    if (viewMode !== "grouped") {
      return null;
    }
    for (const group of engagementGroups) {
      if (group.engagementId != null && expandedEngagements.has(group.engagementId)) {
        return group.engagementId;
      }
    }
    return null;
  }, [engagementGroups, expandedEngagements, viewMode]);
  const reportingStatusProjection = useTaskPanelReportingStatusProjection(
    reportingInventoryEngagementId,
  );

  const taskCountByEngagement = useMemo(() => {
    const counts = new Map<number, number>();
    for (const task of tasks) {
      if (task.engagement_id != null) {
        const id = task.engagement_id;
        counts.set(id, (counts.get(id) ?? 0) + 1);
      }
    }
    return counts;
  }, [tasks]);

  const { taskActionMutation, deleteTaskMutation } = useTaskPanelMutations({
    tasks,
    clearPlanState,
    onTaskActionError: (error) => {
      const presentation = taskAdmissionErrorPresentation(error, "Action failed");
      toast({
        title: presentation.title,
        description: presentation.description,
        variant: "destructive",
      });
    },
    onDeleteSuccess: (taskId) => {
      setDeletingTasks((previous) => {
        const next = new Set(previous);
        next.delete(taskId);
        return next;
      });
      setDeletePhase("");
      if (selectedTask === taskId) {
        setSelectedTask(null);
        setShowContainerMonitor(false);
      }
      toast({ title: "Task deleted", description: "Task has been successfully deleted" });
    },
    onDeleteError: (_taskId, error) => {
      setDeletingTasks((previous) => {
        const next = new Set(previous);
        next.delete(_taskId);
        return next;
      });
      setDeletePhase("");
      toast({ title: "Delete failed", description: error.message, variant: "destructive" });
    },
  });

  const handleTaskAction = (taskId: number, action: string) => {
    if (!canControlTask) {
      toast({
        title: "Action unavailable",
        description: "Your current tenant permissions do not allow runtime control actions.",
        variant: "destructive",
      });
      return;
    }
    taskActionMutation.mutate({ taskId, action });
  };

  const handleDeleteTask = async (taskId: number, taskName: string) => {
    if (!canDeleteTask) {
      toast({
        title: "Delete unavailable",
        description: "Your current tenant permissions do not allow deleting tasks.",
        variant: "destructive",
      });
      return;
    }
    if (deletingTasks.has(taskId)) {
      return;
    }
    if (!window.confirm(`Are you sure you want to delete the task "${taskName}"? This action cannot be undone.`)) {
      return;
    }
    setDeletingTasks((prev) => new Set(prev).add(taskId));
    setDeletePhase(`Deleting "${taskName}"...`);
    try {
      await deleteTaskMutation.mutateAsync(taskId);
    } catch {
      /* onError */
    }
  };

  const toggleContainerMonitor = (taskId: number) => {
    if (!canControlTask) {
      return;
    }
    if (selectedTask === taskId && showContainerMonitor) {
      setShowContainerMonitor(false);
      setSelectedTask(null);
    } else {
      setSelectedTask(taskId);
      setShowContainerMonitor(true);
    }
  };

  const isTaskDeleting = (taskId: number) => deletingTasks.has(taskId) || deleteTaskMutation.isPending;

  const handleRefresh = async () => {
    setIsRefreshing(true);
    await queryClient.invalidateQueries({ queryKey: ["/api/tasks/"] });
    setIsRefreshing(false);
    toast({ title: "Tasks refreshed successfully" });
  };

  const handleViewDetails = (taskId: number, taskName: string) => {
    setScopeDetailsModal({ isOpen: true, taskId, taskName });
  };

  const closeScopeDetailsModal = () => {
    setScopeDetailsModal({ isOpen: false, taskId: null, taskName: "" });
  };

  const handleOpenTerminal = (taskId: number) => {
    openTerminalForTask(taskId);
  };

  const handleAddTaskToEngagement = (engagementId: number) => {
    if (!canCreateTask) {
      toast({
        title: "Action unavailable",
        description: "Your current tenant permissions do not allow creating tasks.",
        variant: "destructive",
      });
      return;
    }
    setPreselectedEngagementId(engagementId);
    setShowNewTaskModal(true);
  };

  const handleArchiveEngagement = async (engagementId: number) => {
    if (!canWriteKnowledge) {
      toast({
        title: "Action unavailable",
        description: "Your current tenant permissions do not allow engagement mutations.",
        variant: "destructive",
      });
      return;
    }
    const group = engagementGroups.find((candidate) => candidate.engagementId === engagementId);
    const name = group?.engagementName ?? `Engagement ${engagementId}`;
    const hasRuntimeActiveTasks = tasks.some(
      (task) =>
        task.engagement_id === engagementId && ARCHIVE_BLOCKING_TASK_STATUSES.has(task.status),
    );
    if (hasRuntimeActiveTasks) {
      toast({
        title: "Archive blocked",
        description:
          "Stop/retire runtime-active tasks before archiving. You do not need to delete stopped, failed, or completed tasks.",
        variant: "destructive",
      });
      return;
    }
    if (!window.confirm(`Archive engagement "${name}"? Knowledge and findings will be preserved.`)) {
      return;
    }
    try {
      await archiveEngagementMutation.mutateAsync(engagementId);
      toast({ title: "Engagement archived", description: name });
    } catch (error) {
      toast({
        title: "Archive failed",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  const handleRestoreEngagement = async (engagementId: number) => {
    if (!canWriteKnowledge) {
      toast({
        title: "Action unavailable",
        description: "Your current tenant permissions do not allow engagement mutations.",
        variant: "destructive",
      });
      return;
    }
    try {
      const result = await restoreEngagementMutation.mutateAsync(engagementId);
      const description = result?.id ? `Engagement ${result.id} restored` : "Engagement restored";
      toast({ title: "Engagement restored", description });
    } catch (error) {
      toast({
        title: "Restore failed",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  const handleOpenReportsWorkspace = (engagementId: number | null | undefined) => {
    if (engagementId == null) {
      return;
    }

    setLocation(`/reports?engagement_id=${encodeURIComponent(String(engagementId))}`);
  };

  const handlePrepareReportingInput = async (taskId: number) => {
    const reportingInputRow = reportingStatusProjection.inputByTaskId.get(taskId);
    const task = tasks.find((candidate) => candidate.id === taskId);
    const engagementId = task?.engagement_id ?? reportingStatusProjection.engagementId;
    if (!reportingInputRow || !canPrepareReportingInput(reportingInputRow) || engagementId == null) {
      return;
    }

    try {
      await prepareTaskMemoMutation.mutateAsync({
        task_id: taskId,
        engagement_id: engagementId,
        regenerate: shouldRegeneratePreparedMemo(reportingInputRow),
      });
    } catch (error) {
      toast({
        title: "Prepare failed",
        description: error instanceof Error && error.message.trim()
          ? error.message
          : "Report input preparation failed.",
        variant: "destructive",
      });
    }
  };

  const renderTaskCard = (task: Task) => {
    const reportingInputRow =
      task.engagement_id === reportingStatusProjection.engagementId
        ? reportingStatusProjection.inputByTaskId.get(task.id)
        : undefined;
    const isPreparableReportingInput = reportingInputRow
      ? canPrepareReportingInput(reportingInputRow)
      : false;

    return (
      <TaskPanelCard
        key={task.id}
        task={task}
        showEngagementLabel={viewMode === "flat"}
        selectedTask={selectedTask}
        showContainerMonitor={showContainerMonitor}
        taskActionPending={taskActionMutation.isPending}
        isRefreshing={isRefreshing}
        canTaskControl={canControlTask}
        canTaskDelete={canDeleteTask}
        reportingInputState={reportingInputRow?.input_state}
        canPrepareReportingInput={isPreparableReportingInput}
        isPreparingReportingInput={
          prepareTaskMemoMutation.isPending && prepareTaskMemoMutation.variables?.task_id === task.id
        }
        isTaskDeleting={isTaskDeleting}
        onToggleMonitor={toggleContainerMonitor}
        onRefresh={handleRefresh}
        onViewDetails={handleViewDetails}
        onDelete={handleDeleteTask}
        onTaskAction={handleTaskAction}
        onOpenTerminal={handleOpenTerminal}
        onPrepareReportingInput={handlePrepareReportingInput}
        onOpenReportsWorkspace={
          task.engagement_id != null ? handleOpenReportsWorkspace : undefined
        }
      />
    );
  };

  const listBody = () => {
    if (isLoading) {
      return <div className="py-8 text-center text-gray-400">Loading tasks...</div>;
    }
    const hasGroupedContent = viewMode === "grouped" && engagementGroups.length > 0;
    if (filteredTasks.length === 0 && !hasGroupedContent) {
      return (
        <div className="py-8 text-center text-gray-400">
          <ListTodo className="mx-auto mb-4 h-12 w-12 opacity-50" />
          <p>{hasNameFilter ? "No tasks or engagements match this name" : "No tasks yet"}</p>
          {hasNameFilter ? null : (
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              <Button
                variant="outline"
                className="border-emerald-600 text-emerald-400 hover:bg-emerald-600/10"
                onClick={() => setShowNewEngagementModal(true)}
                disabled={!canWriteKnowledge}
              >
                New Engagement
              </Button>
              <Button
                variant="outline"
                className="border-blue-600 text-blue-400 hover:bg-blue-600/10"
                onClick={() => {
                  setPreselectedEngagementId(null);
                  setShowNewTaskModal(true);
                }}
                disabled={!canCreateTask}
              >
                Quick Task
              </Button>
            </div>
          )}
        </div>
      );
    }
    if (viewMode === "grouped") {
      return (
        <div className="space-y-2">
          {engagementGroups.map((group) => {
            const expandKey = group.engagementId ?? -1;
            const isExpanded = group.engagementId == null ? expandedEngagements.has(-1) : expandedEngagements.has(group.engagementId);
            return (
              <EngagementSection
                key={`${expandKey}`}
                group={group}
                isExpanded={isExpanded}
                isEngagementMutationPending={
                  archiveEngagementMutation.isPending || restoreEngagementMutation.isPending
                }
                canCreateTask={canCreateTask}
                canMutateEngagement={canWriteKnowledge}
                onToggleExpand={() => toggleEngagementExpanded(expandKey)}
                onAddTask={handleAddTaskToEngagement}
                onArchiveEngagement={handleArchiveEngagement}
                onRestoreEngagement={handleRestoreEngagement}
                renderTaskCard={renderTaskCard}
              />
            );
          })}
        </div>
      );
    }
    return <div className="space-y-2.5">{filteredTasks.map((t) => renderTaskCard(t))}</div>;
  };

  return (
    <div className="flex h-full min-h-0 flex-col border-r border-slate-700 bg-slate-900">
      <TaskPanelToolbar
        viewMode={viewMode}
        onViewMode={setViewMode}
        onNewTask={() => {
          if (!canCreateTask) {
            return;
          }
          setPreselectedEngagementId(null);
          setShowNewTaskModal(true);
        }}
        onNewEngagement={() => {
          if (!canWriteKnowledge) {
            return;
          }
          setShowNewEngagementModal(true);
        }}
        nameFilter={nameFilter}
        onNameFilterChange={setNameFilter}
        canCreateTask={canCreateTask}
        canCreateEngagement={canWriteKnowledge}
      />

      <div className="min-h-0 flex-1 overflow-auto p-3 scrollbar-show-on-hover">
        <div className="mb-2 flex justify-end">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs text-slate-400 hover:text-slate-200"
            onClick={() => setShowArchivedEngagements((prev) => !prev)}
          >
            {showArchivedEngagements ? "Hide archived" : "Show archived"}
          </Button>
        </div>
        {deletePhase ? <div className="px-2 py-1 text-sm text-gray-500">{deletePhase}</div> : null}
        {listBody()}
      </div>

      <NewTaskModal
        open={showNewTaskModal}
        onOpenChange={(open) => {
          setShowNewTaskModal(open);
          if (!open) {
            setPreselectedEngagementId(null);
          }
        }}
        preselectedEngagementId={preselectedEngagementId}
        taskCountByEngagement={taskCountByEngagement}
        canCreateTask={canCreateTask}
      />
      <NewEngagementModal open={showNewEngagementModal} onOpenChange={setShowNewEngagementModal} />

      {scopeDetailsModal.taskId ? (
        <ScopeDetailsModal
          isOpen={scopeDetailsModal.isOpen}
          onClose={closeScopeDetailsModal}
          taskId={scopeDetailsModal.taskId}
          taskName={scopeDetailsModal.taskName}
        />
      ) : null}
    </div>
  );
}
