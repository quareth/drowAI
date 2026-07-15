/**
 * Compact task, model, usage, and stream controls for the chat header.
 *
 * Responsibilities:
 * - render the current task selector and runtime status controls
 * - show usage/context-window indicators for the selected task
 * - expose the current LLM model selection control without owning provider policy
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, Gauge } from "lucide-react";

import {
  findSelectedCatalogEntry,
} from "@/features/llm-provider/catalog";
import {
  getDefaultVisibleReasoningEffort,
  getVisibleReasoningEffortOptions,
} from "@/features/llm-provider/capability-controls";
import ProviderModelMenu from "@/features/llm-provider/ProviderModelMenu";
import type {
  LLMModelCatalogResponse,
  LLMSelectionStatus,
  SelectedLLMModel,
  VisibleLLMReasoningEffort,
} from "@/features/llm-provider/types";
import type { Task, TokenUsage } from "@/types";
import { formatCostUSD, formatTokenCount } from "@/types/usage";
import { apiCall } from "@/lib/api-config";
import { cn } from "@/lib/utils";
import { DrowLogo } from "@/components/ui/drow-logo";
import type { TaskRunStatus } from "@/hooks/useTaskRunState";
import { useContextWindow } from "@/hooks/useContextWindow";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface TaskModelSelectorsProps {
  selectedTaskId: number | null;
  conversationId?: string | null;
  onTaskChange: (taskId: number) => void;
  selectedSelection: SelectedLLMModel | null;
  selectionStatus?: LLMSelectionStatus | null;
  onModelChange: (
    selection: SelectedLLMModel,
    options?: { reasoningEffort?: ReasoningEffort },
  ) => void;
  selectedReasoningEffort: ReasoningEffort;
  onReasoningEffortChange: (effort: ReasoningEffort) => void;
  tasks: Task[];
  llmCatalog: LLMModelCatalogResponse | undefined;
  isConnected: boolean;
  runStates?: Record<number, TaskRunStatus>;
  onDownloadTranscript?: () => void;
  isTranscriptDownloadPending?: boolean;
}

// Use the new TokenUsage type from /api/tasks/{id}/usage endpoint
type UsageResponse = TokenUsage | null;
const DEFAULT_CONTEXT_MAX_TOKENS = 128000;
const USAGE_REFRESH_COOLDOWN_MS = 2500;
export type ReasoningEffort = VisibleLLMReasoningEffort;

interface StreamingStateEventDetail {
  taskId?: number | null;
  task_id?: number | null;
  isStreaming?: boolean | null;
  is_streaming?: boolean | null;
}

interface TaskRunStateEventDetail {
  taskId?: number | null;
  task_id?: number | null;
  state?: string | null;
}

export function TaskModelSelectors({
  selectedTaskId,
  conversationId,
  onTaskChange,
  selectedSelection,
  selectionStatus,
  onModelChange,
  selectedReasoningEffort,
  onReasoningEffortChange,
  tasks,
  llmCatalog,
  isConnected,
  runStates = {},
  onDownloadTranscript,
  isTranscriptDownloadPending = false,
}: TaskModelSelectorsProps) {
  const [showUsage, setShowUsage] = useState(false);
  const [showContextUsage, setShowContextUsage] = useState(false);
  const lastUsageRefreshAtRef = useRef(0);
  const {
    snapshot: contextSnapshot,
    conversationId: contextConversationId,
    refresh: refreshContextWindow,
  } = useContextWindow({
    taskId: selectedTaskId,
    conversationId,
    enabled: Boolean(selectedTaskId),
  });
  const resolvedConversationId =
    typeof contextConversationId === "string" && contextConversationId.trim().length > 0
      ? contextConversationId
      : typeof conversationId === "string" && conversationId.trim().length > 0
        ? conversationId.trim()
        : null;
  const hasConversationSelection = Boolean(resolvedConversationId);

  // Fetch actual token usage from the new /usage endpoint
  const { data: usageData, isFetching: isFetchingUsage, refetch: refetchUsage } = useQuery<UsageResponse>({
    queryKey: selectedTaskId
      ? ["/api/tasks/", selectedTaskId, "/usage"]
      : ["/api/tasks/", "none", "/usage"],
    queryFn: async () => {
      if (!selectedTaskId) return null;
      return apiCall(`/api/tasks/${selectedTaskId}/usage`);
    },
    enabled: Boolean(selectedTaskId),
    // Keep usage cached and refresh only on explicit invalidation (e.g. turn completes).
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const refreshUsage = useCallback(
    (options?: { force?: boolean }) => {
      if (!selectedTaskId) {
        return;
      }
      const now = Date.now();
      const force = Boolean(options?.force);
      if (!force && now - lastUsageRefreshAtRef.current < USAGE_REFRESH_COOLDOWN_MS) {
        return;
      }
      lastUsageRefreshAtRef.current = now;
      void refetchUsage();
    },
    [refetchUsage, selectedTaskId],
  );
  const refreshContext = useCallback(
    (options?: { force?: boolean }) => {
      if (typeof refreshContextWindow === "function") {
        refreshContextWindow(options);
      }
    },
    [refreshContextWindow],
  );

  useEffect(() => {
    lastUsageRefreshAtRef.current = 0;
  }, [selectedTaskId]);

  useEffect(() => {
    if (typeof window === "undefined" || !selectedTaskId) {
      return () => undefined;
    }

    const streamingHandler = (event: Event) => {
      const detail = (event as CustomEvent<StreamingStateEventDetail>).detail ?? {};
      const detailTaskId = Number(
        (detail.taskId as number | null | undefined) ??
        (detail.task_id as number | null | undefined),
      );
      if (!Number.isFinite(detailTaskId) || detailTaskId !== selectedTaskId) {
        return;
      }
      const isStreamingRaw = detail.isStreaming ?? detail.is_streaming;
      if (typeof isStreamingRaw === "boolean" && !isStreamingRaw) {
        refreshUsage({ force: false });
        refreshContext({ force: false });
      }
    };

    const runStateHandler = (event: Event) => {
      const detail = (event as CustomEvent<TaskRunStateEventDetail>).detail ?? {};
      const detailTaskId = Number(
        (detail.taskId as number | null | undefined) ??
        (detail.task_id as number | null | undefined),
      );
      if (!Number.isFinite(detailTaskId) || detailTaskId !== selectedTaskId) {
        return;
      }
      const state = typeof detail.state === "string" ? detail.state.toLowerCase() : "";
      if (
        state === "completed" ||
        state === "waiting_for_human" ||
        state === "declined" ||
        state === "failed" ||
        state === "cancelled"
      ) {
        refreshUsage({ force: false });
        refreshContext({ force: false });
      }
    };

    window.addEventListener("llm-streaming", streamingHandler as EventListener);
    window.addEventListener("task-run-state", runStateHandler as EventListener);
    return () => {
      window.removeEventListener("llm-streaming", streamingHandler as EventListener);
      window.removeEventListener("task-run-state", runStateHandler as EventListener);
    };
  }, [refreshContext, refreshUsage, selectedTaskId]);

  const runningTasks = useMemo(
    () => tasks.filter((task) => task.status === "running"),
    [tasks],
  );

  const sortedByCreated = useMemo(() => {
    const copy = [...tasks];
    copy.sort(
      (a, b) =>
        new Date(b.created_at).getTime() -
        new Date(a.created_at).getTime(),
    );
    return copy;
  }, [tasks]);

  const otherTasks = useMemo(
    () => sortedByCreated.filter((task) => !runningTasks.some((rt) => rt.id === task.id)),
    [sortedByCreated, runningTasks],
  );

  const selectedCatalogEntry = useMemo(
    () => findSelectedCatalogEntry(llmCatalog, selectedSelection),
    [llmCatalog, selectedSelection],
  );
  const blockingSelectionStatus =
    selectionStatus?.runnable === false ? selectionStatus : null;
  const blockingSelectionLabel = useMemo(() => {
    if (!blockingSelectionStatus) {
      return null;
    }
    const prefix =
      blockingSelectionStatus.status === "credential_missing"
        ? "Credential required"
        : blockingSelectionStatus.status === "adapter_unavailable"
          ? "Provider unavailable"
          : blockingSelectionStatus.status === "invalid_selection"
            ? "Invalid selection"
            : "Model unavailable";
    return blockingSelectionStatus.reason
      ? `${prefix}: ${blockingSelectionStatus.reason}`
      : prefix;
  }, [blockingSelectionStatus]);

  const taskLabel = useMemo(() => {
    if (!selectedTaskId) return "No active task";
    const match = tasks.find((task) => task.id === selectedTaskId);
    const runState = selectedTaskId ? runStates[selectedTaskId]?.state : undefined;
    const stateSuffix = runState && runState !== "idle" ? ` - ${runState.replaceAll("_", " ")}` : "";
    return match ? `${match.name} (#${match.id})${stateSuffix}` : `Task #${selectedTaskId}${stateSuffix}`;
  }, [tasks, selectedTaskId, runStates]);

  useEffect(() => {
    const options = getVisibleReasoningEffortOptions(selectedCatalogEntry?.model);
    if (options.length > 0 && !options.includes(selectedReasoningEffort)) {
      onReasoningEffortChange(
        getDefaultVisibleReasoningEffort(selectedCatalogEntry?.model) ?? options[0],
      );
    }
  }, [selectedCatalogEntry?.model, selectedReasoningEffort, onReasoningEffortChange]);

  // Format usage display with input/output breakdown
  const usageDisplay = useMemo(() => {
    if (!usageData) {
      return {
        cost: "$0.00",
        inputTokens: "0",
        outputTokens: "0",
        totalTokens: "0",
        callCount: 0,
        hasData: false,
      };
    }
    
    return {
      cost: formatCostUSD(usageData.cost_usd),
      inputTokens: formatTokenCount(usageData.prompt_tokens),
      outputTokens: formatTokenCount(usageData.completion_tokens),
      totalTokens: formatTokenCount(usageData.total_tokens),
      callCount: usageData.call_count,
      hasData: usageData.call_count > 0,
    };
  }, [usageData]);

  const contextDisplay = useMemo(() => {
    if (!hasConversationSelection) {
      return {
        maxTokens: DEFAULT_CONTEXT_MAX_TOKENS,
        usedTokens: 0,
        ratio: 0,
        percent: 0,
      };
    }
    const maxTokens =
      contextSnapshot.maxTokens > 0
        ? contextSnapshot.maxTokens
        : DEFAULT_CONTEXT_MAX_TOKENS;
    const usedTokens = Math.max(0, Math.min(contextSnapshot.usedTokens, maxTokens));
    const ratioFromTokens = maxTokens > 0 ? usedTokens / maxTokens : 0;
    const ratio = Math.max(
      0,
      Math.min(
        1,
        contextSnapshot.ratio > 0 ? contextSnapshot.ratio : ratioFromTokens,
      ),
    );
    const percent = Math.round(ratio * 100);
    return {
      maxTokens,
      usedTokens,
      ratio,
      percent,
    };
  }, [
    contextSnapshot.maxTokens,
    contextSnapshot.ratio,
    contextSnapshot.usedTokens,
    hasConversationSelection,
  ]);
  const contextTooltipText = useMemo(
    () => `Context window: ${contextDisplay.percent}%`,
    [contextDisplay.percent],
  );

  return (
    <div className="bg-slate-900/30 border-b border-slate-800/30 px-3 py-1.5 flex items-center justify-between shrink-0">
      <div className="flex items-center space-x-2">
        <div className="w-6 h-6 flex items-center justify-center">
          <DrowLogo size={18} />
        </div>
        <div>
          <div className="flex items-center space-x-1.5">
            <h3 className="font-medium text-slate-200 text-xs tracking-tight">Command Post</h3>
            <div className="relative">
              <div className="group flex items-center gap-1">
                <Gauge
                  className="w-3 h-3 text-slate-400 hover:text-slate-200 cursor-pointer transition-colors"
                  onMouseEnter={() => {
                    refreshUsage({ force: false });
                    setShowUsage(true);
                  }}
                  onMouseLeave={() => setShowUsage(false)}
                  aria-label="Task usage metrics"
                />
                <div
                  className="absolute top-full left-1/2 transform -translate-x-1/2 mt-2 px-3 py-2 bg-slate-800 text-slate-200 text-xs rounded shadow-lg transition-opacity z-50 pointer-events-none min-w-[180px]"
                  style={{ opacity: showUsage ? 1 : 0 }}
                >
                  {isFetchingUsage ? (
                    <span className="text-slate-400">Calculating…</span>
                  ) : usageDisplay.hasData ? (
                    <div className="space-y-1">
                      <div className="font-medium text-slate-100 border-b border-slate-700 pb-1 mb-1">
                        Token Usage
                      </div>
                      <div className="flex justify-between">
                        <span className="text-slate-400">Input:</span>
                        <span className="font-mono">{usageDisplay.inputTokens}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-slate-400">Output:</span>
                        <span className="font-mono">{usageDisplay.outputTokens}</span>
                      </div>
                      <div className="flex justify-between border-t border-slate-700 pt-1 mt-1">
                        <span className="text-slate-400">Total:</span>
                        <span className="font-mono">{usageDisplay.totalTokens}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-slate-400">Cost:</span>
                        <span className="font-mono text-emerald-400">{usageDisplay.cost}</span>
                      </div>
                      <div className="flex justify-between text-[10px] text-slate-500 pt-1">
                        <span>API calls:</span>
                        <span>{usageDisplay.callCount}</span>
                      </div>
                    </div>
                  ) : (
                    <span className="text-slate-400">No usage recorded yet</span>
                  )}
                  <div className="absolute bottom-full left-1/2 transform -translate-x-1/2 w-0 h-0 border-l-4 border-r-4 border-b-4 border-transparent border-b-slate-800" />
                </div>
                <div
                  className="h-3 w-3 rounded-full cursor-pointer transition-transform hover:scale-105"
                  style={{
                    background: `conic-gradient(from -90deg, rgb(16 185 129) ${contextDisplay.percent}%, rgb(51 65 85) ${contextDisplay.percent}% 100%)`,
                    WebkitMask:
                      "radial-gradient(farthest-side, transparent calc(100% - 1.5px), black calc(100% - 1.5px))",
                    mask: "radial-gradient(farthest-side, transparent calc(100% - 1.5px), black calc(100% - 1.5px))",
                  }}
                  onMouseEnter={() => {
                    refreshContext({ force: false });
                    setShowContextUsage(true);
                  }}
                  onMouseLeave={() => setShowContextUsage(false)}
                  aria-label={`Context window usage ${contextDisplay.percent}%`}
                />
                <div
                  className="absolute top-full left-1/2 transform -translate-x-1/2 mt-2 px-3 py-2 bg-slate-800 text-slate-200 text-xs rounded shadow-lg transition-opacity z-50 pointer-events-none min-w-[180px]"
                  style={{ opacity: showContextUsage ? 1 : 0 }}
                >
                  <span className="text-slate-200">{contextTooltipText}</span>
                  <div className="absolute bottom-full left-1/2 transform -translate-x-1/2 w-0 h-0 border-l-4 border-r-4 border-b-4 border-transparent border-b-slate-800" />
                </div>
              </div>
            </div>
          </div>
          <p className="text-[10px] text-slate-400">{taskLabel}</p>
        </div>
      </div>

      <div className="flex items-center space-x-1.5">
        <div className="mr-2 flex max-w-[260px] flex-col items-end gap-0.5">
          <ProviderModelMenu
            catalog={llmCatalog}
            selectedSelection={selectedSelection}
            selectedReasoningEffort={selectedReasoningEffort}
            onModelChange={onModelChange}
          />
          {blockingSelectionLabel && (
            <p
              role="alert"
              className="max-w-full truncate text-[10px] font-medium text-red-300"
              title={blockingSelectionLabel}
            >
              {blockingSelectionLabel}
            </p>
          )}
        </div>


        <div className="mr-2">
          <Select
            value={selectedTaskId != null ? String(selectedTaskId) : undefined}
            onValueChange={(value) => {
              const parsed = Number(value);
              if (Number.isNaN(parsed)) return;
              onTaskChange(parsed);
            }}
          >
            <SelectTrigger
              aria-label="Select task"
              className="h-7 min-w-[113px] px-2.5 py-1 text-xs"
            >
              <SelectValue placeholder="Select task" />
            </SelectTrigger>
            <SelectContent>
              {runningTasks.map((task) => (
                <SelectItem key={task.id} value={String(task.id)} className="text-xs">
                  {task.name} (#{task.id}){runStates[task.id]?.state && runStates[task.id]?.state !== "idle" ? ` - ${runStates[task.id]?.state.replaceAll("_", " ")}` : ""}
                </SelectItem>
              ))}
              {otherTasks.map((task) => (
                <SelectItem key={task.id} value={String(task.id)} className="text-xs">
                  {task.name} (#{task.id}){runStates[task.id]?.state && runStates[task.id]?.state !== "idle" ? ` - ${runStates[task.id]?.state.replaceAll("_", " ")}` : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center">
          <span className="sr-only">
            {isConnected ? "Stream connected" : "Stream disconnected"}
          </span>
          <div
            className={cn(
              "w-1.5 h-1.5 rounded-full",
              isConnected ? "bg-emerald-500" : "bg-red-500",
            )}
            aria-hidden="true"
          />
        </div>
        <button
          type="button"
          className="p-1 text-slate-400 hover:text-slate-200 hover:bg-slate-800/30 rounded transition-colors disabled:cursor-not-allowed disabled:opacity-40"
          onClick={onDownloadTranscript}
          disabled={!selectedTaskId || !onDownloadTranscript || isTranscriptDownloadPending}
          aria-label="Download transcript"
          title="Download transcript"
        >
          <Download className="w-3 h-3" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

export default TaskModelSelectors;
