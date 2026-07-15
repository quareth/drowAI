import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  ClipboardList,
  CheckCircle2,
  Circle,
  Loader2,
  Pencil,
  Play,
  Trash2,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { usePlanContext } from "@/contexts/PlanContext";
import { useGraphResume } from "@/hooks/useGraphResume";
import { useInterruptState, type SetInterruptOptions } from "@/hooks/useInterruptState";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import {
  isPlanReviewPayload,
  type GraphInterruptEventDetail,
  type PlanReviewPayload,
} from "@/types/hitl";

interface PlanCardProps {
  taskId: number;
  className?: string;
  interruptState?: PlanCardInterruptState;
}

interface PlanCardInterruptState {
  interrupt: GraphInterruptEventDetail | null;
  refetch: () => Promise<void>;
  setInterrupt: (
    detail: GraphInterruptEventDetail | null,
    options?: SetInterruptOptions,
  ) => void;
}

interface PlanCardContentProps {
  taskId: number;
  className?: string;
  interruptState: PlanCardInterruptState;
}

const STEP_PREFIX_PATTERN = /^step\s+\d+\s*[:.-]?\s*/i;

function stripStepPrefix(step: string): string {
  return step.replace(STEP_PREFIX_PATTERN, "").trim();
}

function normalizeSteps(steps: string[]): string[] {
  return steps.map(stripStepPrefix);
}

function formatPlanSteps(steps: string[]): string[] {
  return steps.map((step, index) => {
    const content = stripStepPrefix(step);
    return content ? `Step ${index + 1}: ${content}` : `Step ${index + 1}`;
  });
}

function resizeTextareaToContent(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

function PlanCardWithLocalInterrupt({ taskId, className }: PlanCardProps) {
  const interruptState = useInterruptState(taskId);
  return (
    <PlanCardContent
      taskId={taskId}
      className={className}
      interruptState={interruptState}
    />
  );
}

function PlanCardContent({ taskId, className, interruptState }: PlanCardContentProps) {
  const { interrupt, refetch, setInterrupt } = interruptState;
  const {
    setPlan,
    setActiveTask,
    setPlanCardMinimized,
    rejectRun,
    updatePlan,
    getTaskState,
    getTaskUiState,
  } = usePlanContext();
  const { mutateAsync: resumeGraph, isPending } = useGraphResume();
  const { toast } = useToast();

  const [expanded, setExpanded] = useState(false);
  const [editMode, setEditMode] = useState(false);
  const [editedSteps, setEditedSteps] = useState<string[]>([]);
  const [activeEditStepIndex, setActiveEditStepIndex] = useState<number | null>(null);
  const editStepTextareaRefs = useRef<Array<HTMLTextAreaElement | null>>([]);

  const planInterrupt = useMemo(() => {
    if (interrupt && isPlanReviewPayload(interrupt.payload)) {
      return interrupt;
    }
    return null;
  }, [interrupt]);

  // Type narrowing: planInterrupt is only set when payload is PlanReviewPayload
  const planPayload = planInterrupt?.payload as PlanReviewPayload | null;
  const currentRun = getTaskState(taskId).currentRun;
  const { isPlanCardMinimized } = getTaskUiState(taskId);

  useEffect(() => {
    setActiveTask(taskId);
  }, [setActiveTask, taskId]);

  useEffect(() => {
    if (!planPayload) return;
    if (!currentRun || currentRun.runId !== (planPayload.run_id ?? currentRun.runId)) {
      setPlan(taskId, planPayload);
    }
  }, [planPayload, currentRun, setPlan, taskId]);

  useEffect(() => {
    if (!editMode || activeEditStepIndex === null) return;
    const activeTextarea = editStepTextareaRefs.current[activeEditStepIndex];
    if (!activeTextarea) return;
    activeTextarea.focus({ preventScroll: true });
    resizeTextareaToContent(activeTextarea);
  }, [activeEditStepIndex, editMode]);

  useEffect(() => {
    if (!editMode || activeEditStepIndex === null) return;
    const activeTextarea = editStepTextareaRefs.current[activeEditStepIndex];
    if (!activeTextarea) return;
    resizeTextareaToContent(activeTextarea);
  }, [activeEditStepIndex, editMode, editedSteps]);

  const todoList = currentRun?.todoList ?? planPayload?.todo_list ?? [];
  const displaySteps = currentRun?.planSteps ?? planPayload?.plan_steps ?? [];
  const normalizedSteps = normalizeSteps(displaySteps);
  const stepsForDisplay = editMode ? editedSteps : normalizedSteps;
  const stepStatuses = useMemo(() => {
    if (editMode) {
      return stepsForDisplay.map(() => "pending");
    }
    return stepsForDisplay.map((_, index) => todoList[index]?.status ?? "pending");
  }, [editMode, stepsForDisplay, todoList]);
  const isInterrupted = planInterrupt !== null && (!currentRun || currentRun.status === "interrupted");

  if (!planPayload && !currentRun) {
    return null;
  }

  const handleApprove = async () => {
    if (!planInterrupt) return;
    if (planInterrupt.taskId !== taskId) return;
    
    // Dismiss immediately to prevent duplicate submissions
    const interruptData = { ...planInterrupt };
    setInterrupt(null);
    
    try {
      await resumeGraph({
        taskId: interruptData.taskId,
        interruptType: "plan_review",
        interruptId: interruptData.interruptId,
        graphName: interruptData.graphName,
        response: { action: "approve" },
      });
      await refetch();
    } catch (error) {
      setInterrupt(interruptData, { allowDismissedReveal: true });
      console.error("[PlanCard] Approve failed:", error);
      const description = error instanceof Error ? error.message : "Please try again.";
      toast({ title: "Plan approval failed", description });
      // Refetch to restore state on error
      await refetch();
    }
  };

  const handleEdit = async () => {
    if (!planInterrupt) return;
    if (planInterrupt.taskId !== taskId) return;
    
    // Capture current edit state and dismiss immediately to prevent duplicate submissions
    const interruptData = { ...planInterrupt };
    const stepsToSend = formatPlanSteps(
      editedSteps.length > 0 ? editedSteps : normalizedSteps,
    );
    
    setInterrupt(null);
    setEditMode(false);
    setActiveEditStepIndex(null);
    
    // Update context with edited values so card shows the new plan immediately
    updatePlan(taskId, undefined, stepsToSend);
    
    try {
      await resumeGraph({
        taskId: interruptData.taskId,
        interruptType: "plan_review",
        interruptId: interruptData.interruptId,
        graphName: interruptData.graphName,
        response: {
          action: "edit",
          edited_plan_steps: stepsToSend,
        },
      });
      await refetch();
    } catch (error) {
      setInterrupt(interruptData, { allowDismissedReveal: true });
      console.error("[PlanCard] Edit failed:", error);
      const description = error instanceof Error ? error.message : "Please try again.";
      toast({ title: "Plan edit failed", description });
      // Refetch to restore state on error
      await refetch();
    }
  };

  const handleReject = async () => {
    if (!planInterrupt) return;
    if (planInterrupt.taskId !== taskId) return;
    
    // Dismiss immediately to prevent duplicate submissions
    const interruptData = { ...planInterrupt };
    setInterrupt(null);
    setEditMode(false);
    setActiveEditStepIndex(null);

    try {
      await resumeGraph({
        taskId: interruptData.taskId,
        interruptType: "plan_review",
        interruptId: interruptData.interruptId,
        graphName: interruptData.graphName,
        response: { action: "reject" },
      });
      rejectRun(taskId);
      await refetch();
    } catch (error) {
      setInterrupt(interruptData, { allowDismissedReveal: true });
      console.error("[PlanCard] Reject failed:", error);
      const description = error instanceof Error ? error.message : "Please try again.";
      toast({ title: "Plan rejection failed", description });
      // Refetch to restore state on error
      await refetch();
    }
  };

  const startEdit = () => {
    setEditedSteps([...normalizedSteps]);
    setActiveEditStepIndex(null);
    setEditMode(true);
    setExpanded(true);
  };

  if (isPlanCardMinimized) {
    return (
      <div
        className={cn(
          "fixed bottom-20 right-4 z-50",
          "rounded-lg border border-slate-700/40 bg-slate-900/95",
          "shadow-xl backdrop-blur-sm",
          className,
        )}
      >
        <div className="flex items-center justify-between px-3 py-2">
          <div className="flex items-center gap-2">
            <ClipboardList className="h-4 w-4 text-emerald-400" />
            <span className="text-sm font-medium text-slate-200">Plan</span>
            {isInterrupted && (
              <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
                Review
              </span>
            )}
          </div>
          <button
            onClick={() => setPlanCardMinimized(taskId, false)}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-300"
          >
            <ChevronUp className="h-4 w-4" />
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "fixed bottom-20 right-4 z-50 w-80",
        "rounded-lg border border-slate-700/40 bg-slate-900/95",
        "shadow-xl backdrop-blur-sm",
        className,
      )}
    >
      <div className="flex items-center justify-between border-b border-slate-700/30 px-3 py-2">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-4 w-4 text-emerald-400" />
          <span className="text-sm font-medium text-slate-200">Plan</span>
          {isInterrupted && (
            <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
              Review
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setExpanded(!expanded)}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-300"
          >
            {expanded ? (
              <ChevronUp className="h-4 w-4" />
            ) : (
              <ChevronDown className="h-4 w-4" />
            )}
          </button>
          <button
            onClick={() => setPlanCardMinimized(taskId, true)}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-300"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="max-h-[60vh] overflow-y-auto p-3">
        {expanded && (
          <div>
            <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
              Steps ({normalizedSteps.length})
            </span>
            <ul className="mt-1 space-y-1">
              {stepsForDisplay.map((step, idx) => (
                <li
                  key={idx}
                  className="flex min-w-0 items-start gap-2 text-xs text-slate-400"
                >
                  {editMode ? (
                    <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-slate-800 text-[10px] text-slate-500">
                      {idx + 1}
                    </span>
                  ) : stepStatuses[idx] === "completed" ? (
                    <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                  ) : stepStatuses[idx] === "in_progress" ? (
                    <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-amber-400" />
                  ) : (
                    <Circle className="mt-0.5 h-4 w-4 shrink-0 text-slate-600" />
                  )}
                  {editMode ? (
                    <div className="flex min-w-0 flex-1 items-start gap-2">
                      {activeEditStepIndex === idx ? (
                        <textarea
                          ref={(element) => {
                            editStepTextareaRefs.current[idx] = element;
                          }}
                          value={editedSteps[idx] ?? ""}
                          onChange={(e) => {
                            const newSteps = [...editedSteps];
                            newSteps[idx] = e.target.value;
                            setEditedSteps(newSteps);
                            resizeTextareaToContent(e.target);
                          }}
                          onFocus={() => setActiveEditStepIndex(idx)}
                          rows={3}
                          className="w-full min-w-0 flex-1 resize-none rounded border border-emerald-500/40 bg-slate-950 px-2 py-1 text-xs leading-relaxed text-slate-200 focus:outline-none focus:ring-1 focus:ring-emerald-500/40"
                        />
                      ) : (
                        <button
                          type="button"
                          onClick={() => setActiveEditStepIndex(idx)}
                          className="w-full min-w-0 flex-1 overflow-hidden rounded border border-slate-700 bg-slate-950 px-2 py-0.5 text-left text-xs text-slate-300 hover:border-slate-600 focus:outline-none focus:ring-1 focus:ring-emerald-500/40"
                          title={editedSteps[idx] ?? ""}
                          aria-label={`Edit step ${idx + 1}`}
                        >
                          <span className="block truncate leading-6">
                            {editedSteps[idx] ?? ""}
                          </span>
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          if (editedSteps.length <= 1) return;
                          setEditedSteps((prev) => prev.filter((_, index) => index !== idx));
                          setActiveEditStepIndex((current) => {
                            if (current === null) return null;
                            if (current === idx) {
                              const nextLength = editedSteps.length - 1;
                              return nextLength > 0 ? Math.min(idx, nextLength - 1) : null;
                            }
                            if (current > idx) {
                              return current - 1;
                            }
                            return current;
                          });
                        }}
                        disabled={editedSteps.length <= 1}
                        className="shrink-0 rounded p-1 text-slate-500 hover:bg-slate-800 hover:text-slate-300 disabled:cursor-not-allowed disabled:text-slate-700"
                        aria-label={`Remove step ${idx + 1}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : (
                    <span
                      className={cn(
                        "leading-relaxed",
                        stepStatuses[idx] === "completed"
                          ? "text-slate-500 line-through"
                          : stepStatuses[idx] === "in_progress"
                            ? "text-slate-300"
                            : "text-slate-400",
                      )}
                    >
                      {step}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {isInterrupted && (
        <div className="flex items-center gap-2 border-t border-slate-700/30 px-3 py-2">
          <Button
            size="sm"
            onClick={editMode ? handleEdit : handleApprove}
            disabled={isPending}
            className="h-7 bg-emerald-600 px-3 text-xs hover:bg-emerald-700"
          >
            <Play className="mr-1 h-3 w-3" />
            {isPending ? "Resuming..." : editMode ? "Save & Run" : "Run"}
          </Button>
          {!editMode && (
            <Button
              size="sm"
              variant="outline"
              onClick={startEdit}
              disabled={isPending}
              className="h-7 px-3 text-xs text-white bg-[rgba(10,14,31,0.1)]"
            >
              <Pencil className="mr-1 h-3 w-3" />
              Edit
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={handleReject}
            disabled={isPending}
            className="h-7 px-3 text-xs text-slate-400 hover:text-slate-300"
          >
            <X className="mr-1 h-3 w-3" />
            Reject
          </Button>
        </div>
      )}
    </div>
  );
}

export function PlanCard(props: PlanCardProps) {
  if (props.interruptState) {
    return (
      <PlanCardContent
        taskId={props.taskId}
        className={props.className}
        interruptState={props.interruptState}
      />
    );
  }
  return <PlanCardWithLocalInterrupt taskId={props.taskId} className={props.className} />;
}

export default PlanCard;
