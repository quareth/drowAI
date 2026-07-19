/**
 * Unified chat surface for Overview/task views.
 *
 * Responsibilities:
 * - reconcile active task selection against the current task list
 * - bootstrap and render transcript/chat controls for the selected task
 * - orchestrate send/interrupt flows for interactive chat mode
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useToast } from "@/hooks/use-toast";
import { downloadTextFile } from "@/lib/browser-download";
import { apiRequest } from "@/lib/queryClient";
import {
  fetchLLMModelCatalog,
  fetchLLMSelection,
  saveLLMDeploymentSelection,
  saveLLMSelection,
} from "@/features/llm-provider/api";
import {
  getBlockingSelectionStatus,
  getFirstCatalogDefaultSelection,
  sameDeploymentRef,
} from "@/features/llm-provider/catalog";
import { getSupportedReasoningEffortForPayload } from "@/features/llm-provider/capability-controls";
import type {
  LLMModelCatalogResponse,
  LLMSelection,
  SelectedLLMModel,
} from "@/features/llm-provider/types";
import { useUnifiedChat } from "@/hooks/useUnifiedChat";
import { useSendQueue } from "@/hooks/useSendQueue";
import { useGraphResume } from "@/hooks/useGraphResume";
import { useGraphRetry } from "@/hooks/useGraphRetry";
import { useChatStop } from "@/hooks/useChatStop";
import { useInterruptState } from "@/hooks/useInterruptState";
import QueueIndicator from "./QueueIndicator";

import MessageList from "./MessageList";
import ChatInput from "./ChatInput";
import TaskModelSelectors from "./TaskModelSelectors";
import type { ReasoningEffort } from "./TaskModelSelectors";
import ToolApprovalCard, { type BatchApprovalDecisions } from "./ToolApprovalCard";
import ClarifyRequiredCard from "./ClarifyRequiredCard";
import { PlanCard } from "@/components/panels/PlanCard";
import { createModeStrategy, createBasicChatStrategy } from "./mode-strategies";
import {
  InteractiveModeOrchestration,
  type ModeOrchestrationContract,
} from "./mode-orchestration";
import type { ChatMode, ChatExperienceMode, ChatPrimaryMode, ChatMessage } from "./types";
import {
  chatExperienceModeToComposite,
  compositeToChatExperienceMode,
  chatSelectionToAgentModePayload,
} from "./types";
import { featureFlags } from "@/config/feature-flags";
import type { Task } from "@/types";
import { useChatBootstrap } from "@/hooks/useChatBootstrap";
import { useTaskRunState } from "@/hooks/useTaskRunState";
import { useTaskNotifications } from "@/hooks/useTaskNotifications";
import { buildChatTranscriptExport } from "@/utils/chatTranscriptExport";
import {
  isClarifyRequestInterruptDetail,
  isToolApprovalInterruptDetail,
  type ClarifyRequestInterruptDetail,
  type ToolApprovalInterruptDetail,
} from "@/types/hitl";
import { setConversationId, useChatSessionSnapshot } from "@/state/chat-session-store";
import { useContextCompactionGate } from "@/state/context-window-store";
import { getActiveChatTaskId, setActiveChatTaskId, useActiveChatTaskId } from "@/state/active-chat-task-store";
import {
  applyRetryStateUpdate,
  getRetryStateForTurn,
  useRetryStateStoreVersion,
} from "@/state/retry-state-store";
import type { MessageBubbleRetryState } from "./MessageBubble";

const deterministicE2EMode = import.meta.env.VITE_E2E_DETERMINISTIC_MODE === "true";

function hasSameLLMSelectionIdentity(
  nextSelection: SelectedLLMModel | LLMSelection,
  currentSelection: SelectedLLMModel | null,
): boolean {
  if (
    !currentSelection
    || nextSelection.provider !== currentSelection.provider
    || nextSelection.model !== currentSelection.model
  ) {
    return false;
  }
  if (nextSelection.deploymentRef || currentSelection.deploymentRef) {
    return sameDeploymentRef(nextSelection.deploymentRef, currentSelection.deploymentRef);
  }
  return true;
}

interface UnifiedAgentChatProps {
  taskId: number | null;
  chatMode?: ChatExperienceMode;
  onChatModeChange?: (mode: ChatExperienceMode) => void;
  onTaskChange?: (taskId: number) => void;
  headerSlot?: ReactNode;
}

export function UnifiedAgentChat({
  taskId,
  chatMode = "agent",
  onChatModeChange,
  onTaskChange,
  headerSlot,
}: UnifiedAgentChatProps) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const currentChatMode: ChatExperienceMode = chatMode;
  const resumeMutation = useGraphResume();
  const retryMutation = useGraphRetry();
  const resumingInterruptIdsRef = useRef<Set<string>>(new Set());

  // Phase 6: convert the legacy flat ``ChatExperienceMode`` prop into
  // the composite (primary, plan) selection used by the new UI. The
  // composite is the canonical internal representation; the legacy
  // prop shape is kept for the parent contract only.
  const selection = useMemo(
    () => chatExperienceModeToComposite(currentChatMode),
    [currentChatMode],
  );
  const primaryMode: ChatPrimaryMode = selection.primary;
  const planMode: boolean = selection.plan;

  const handlePrimaryModeChange = useCallback(
    (next: ChatPrimaryMode) => {
      if (!onChatModeChange) return;
      // Phase 6 Task 6.8: selecting ``chat`` clears Plan — chat and
      // plan are mutually exclusive, and the backend enforces the
      // same invariant.
      const nextPlan = next === "chat" ? false : selection.plan;
      onChatModeChange(
        compositeToChatExperienceMode({ primary: next, plan: nextPlan }),
      );
    },
    [onChatModeChange, selection.plan],
  );

  const handlePlanToggle = useCallback(
    (next: boolean) => {
      if (!onChatModeChange) return;
      // Plan cannot stack with chat (UX contract). Defensive guard
      // keeps component state self-consistent even if a caller
      // bypasses the dropdown's own disable logic.
      if (selection.primary === "chat" && next) {
        return;
      }
      onChatModeChange(
        compositeToChatExperienceMode({
          primary: selection.primary,
          plan: next,
        }),
      );
    },
    [onChatModeChange, selection.primary],
  );

  const agentModeTransport = useMemo(
    () => chatSelectionToAgentModePayload(selection),
    [selection],
  );

  const { data: tasks = [] } = useQuery<Task[]>({
    queryKey: ["/api/tasks/"],
  });

  const { data: llmCatalog } = useQuery<LLMModelCatalogResponse>({
    queryKey: ["/api/llm/models"],
    queryFn: fetchLLMModelCatalog,
  });

  const { data: llmSelection, isFetched: isLLMSelectionFetched } = useQuery<LLMSelection>({
    queryKey: ["/api/llm/selection"],
    queryFn: fetchLLMSelection,
  });

  const [selectedLLMModel, setSelectedLLMModel] = useState<SelectedLLMModel | null>(null);
  const [selectedReasoningEffort, setSelectedReasoningEffort] =
    useState<ReasoningEffort>("medium");
  const [isTranscriptExporting, setIsTranscriptExporting] = useState(false);
  useEffect(() => {
    if (llmSelection?.model) {
      setSelectedLLMModel((currentSelection) => {
        if (hasSameLLMSelectionIdentity(llmSelection, currentSelection)) {
          return currentSelection;
        }
        return {
          provider: llmSelection.provider,
          model: llmSelection.model,
          ...(llmSelection.deploymentRef ? { deploymentRef: llmSelection.deploymentRef } : {}),
        };
      });
    }
  }, [llmSelection]);

  useEffect(() => {
    if (!selectedLLMModel && isLLMSelectionFetched && !llmSelection?.model) {
      const fallbackSelection = getFirstCatalogDefaultSelection(llmCatalog);
      if (fallbackSelection) {
        setSelectedLLMModel(fallbackSelection);
      }
    }
  }, [isLLMSelectionFetched, llmSelection?.model, selectedLLMModel, llmCatalog]);

  const blockedSelectionStatus = useMemo(() => {
    return getBlockingSelectionStatus(llmSelection, selectedLLMModel);
  }, [llmSelection, selectedLLMModel]);

  const blockedSelectionMessage = blockedSelectionStatus?.reason
    ?? "Choose an enabled provider and model before sending a message.";

  const activeChatTaskId = useActiveChatTaskId();
  const activeTaskId = taskId ?? activeChatTaskId;
  const runStateTaskIds = useMemo(() => {
    const ids = tasks
      .filter((task) => task.status === "running")
      .map((task) => task.id);
    if (activeTaskId != null && !ids.includes(activeTaskId)) {
      ids.push(activeTaskId);
    }
    return ids;
  }, [tasks, activeTaskId]);
  const runStates = useTaskRunState(runStateTaskIds);
  const activeRunState = activeTaskId != null ? runStates[activeTaskId] : undefined;
  const isChatGenerationActive = Boolean(activeRunState?.isActiveGeneration);
  const isChatRunStopping = Boolean(isChatGenerationActive && activeRunState?.cancelRequested);
  const stopChatMutation = useChatStop();

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

  useEffect(() => {
    if (taskId != null) {
      if (activeChatTaskId !== taskId) {
        setActiveChatTaskId(taskId);
      }
      return;
    }

    if (tasks.length === 0) {
      if (activeChatTaskId !== null) {
        setActiveChatTaskId(null);
      }
      return;
    }

    const activeExists =
      activeChatTaskId != null && tasks.some((candidate) => candidate.id === activeChatTaskId);
    if (activeExists) {
      return;
    }

    const next = runningTasks[0]?.id ?? sortedByCreated[0]?.id ?? null;
    if (next !== activeChatTaskId) {
      setActiveChatTaskId(next);
    }
  }, [taskId, activeChatTaskId, runningTasks, sortedByCreated, tasks]);

  useEffect(() => {
    const normalizedTaskId = activeTaskId ?? null;
    setActiveChatTaskId(normalizedTaskId);
    return () => {
      if (taskId == null && getActiveChatTaskId() === normalizedTaskId) {
        setActiveChatTaskId(null);
      }
    };
  }, [activeTaskId, taskId]);

  useTaskNotifications({
    activeTaskId,
    tasks,
    runStates,
    notify: ({ title, description, variant }) => toast({ title, description, variant }),
  });
  
  // Use API-backed interrupt hook - queries backend checkpointer for state
  // This is the ONLY source of truth for interrupt state
  // IMPORTANT: Use activeTaskId to ensure we query for the actually displayed task
  const {
    interrupt: pendingInterrupt,
    setInterrupt: setPendingInterrupt,
    refetch: refetchInterrupt,
  } = useInterruptState(activeTaskId);

  const activeTask = useMemo(
    () => (activeTaskId != null ? tasks.find((candidate) => candidate.id === activeTaskId) ?? null : null),
    [tasks, activeTaskId],
  );
  const isTaskRunning = useMemo(() => {
    if (!activeTaskId) {
      return false;
    }
    if (!activeTask || typeof activeTask.status !== "string") {
      return true;
    }
    return activeTask.status.toLowerCase() === "running";
  }, [activeTask, activeTaskId]);
  const executionMode = "interactive" as const;
  const chatSession = useChatSessionSnapshot(activeTaskId ?? null);
  const currentConversationId = chatSession.conversationId ?? null;
  const contextCompactionGate = useContextCompactionGate(
    activeTaskId ?? null,
    currentConversationId,
  );
  const isContextCompacting = Boolean(contextCompactionGate?.active);
  const chatBootstrap = useChatBootstrap({
    taskId: activeTaskId ?? null,
    enabled: featureFlags.enableBasicChat,
  });

  const sendMessageMutation = useMutation({
    mutationFn: async (input: string | { message: string; client_message_id?: string }) => {
      if (!activeTaskId) {
        throw new Error("No task selected");
      }
      const rawMessage = typeof input === "string" ? input : input.message;
      const trimmed = rawMessage.trim();
      if (!trimmed) {
        throw new Error("Message cannot be empty");
      }
      if (!selectedLLMModel) {
        throw new Error("Choose a provider and model before sending a message.");
      }
      if (blockedSelectionStatus) {
        throw new Error(blockedSelectionMessage);
      }
      const clientMessageId =
        typeof input === "string" ? undefined : typeof input.client_message_id === "string" ? input.client_message_id : undefined;

      const reasoningEffort = getSupportedReasoningEffortForPayload(
        llmCatalog,
        selectedLLMModel,
        selectedReasoningEffort,
      );
      const path = `/api/tasks/${activeTaskId}/chat`;
      const payload = {
        message: trimmed,
        deterministic: deterministicE2EMode || undefined,
        conversation_id: currentConversationId ?? undefined,
        provider: selectedLLMModel?.provider,
        model: selectedLLMModel?.model,
        deployment_ref: selectedLLMModel?.deploymentRef ?? undefined,
        agent_mode: agentModeTransport.agent_mode,
        plan_mode: agentModeTransport.plan_mode,
        client_message_id: clientMessageId,
        reasoning_effort: reasoningEffort,
      };
      const response = await apiRequest("POST", path, payload);
      if (!response.ok) {
        const detail = await response.text().catch(() => "Failed to send message");
        throw new Error(detail || "Failed to send message");
      }
      const json = await response.json().catch(() => null as any);
      // Capture conversation id returned by chat endpoint for subsequent turns
      if (json && typeof json.conversation_id === "string" && activeTaskId != null) {
        setConversationId(activeTaskId, json.conversation_id);
      }
      if (
        json &&
        json.queued !== true &&
        typeof json.turn_id === "string" &&
        activeTaskId != null &&
        typeof window !== "undefined"
      ) {
        window.dispatchEvent(
          new CustomEvent("task-run-state", {
            detail: {
              taskId: activeTaskId,
              state: "running",
              turnId: json.turn_id,
              cancelRequested: false,
            },
          }),
        );
        window.dispatchEvent(
          new CustomEvent("llm-streaming", {
            detail: {
              taskId: activeTaskId,
              isStreaming: true,
              queuedCount: 0,
            },
          }),
        );
        void queryClient.invalidateQueries({ queryKey: ["task-run-state-batch"] });
      }
      return json;
    },
  });

  const sseConnection = useMemo(
    () => ({
      isConnected: true,
      reconnect: () => Promise.resolve(),
      disconnect: () => Promise.resolve(),
    }),
    [],
  );

  const interactiveSend = useCallback(
    async (message: string) => {
      await sendMessageMutation.mutateAsync(message);
    },
    [sendMessageMutation],
  );

  const handleStopChat = useCallback(async () => {
    if (!activeTaskId || !activeRunState?.canStop) {
      return;
    }
    try {
      await stopChatMutation.mutateAsync({
        taskId: activeTaskId,
        turnId: activeRunState?.turnId ?? null,
        reason: "user_stop",
      });
    } catch (error) {
      toast({
        title: "Stop failed",
        description: error instanceof Error ? error.message : "Could not stop the active chat run.",
        variant: "destructive",
      });
    }
  }, [activeRunState?.canStop, activeRunState?.turnId, activeTaskId, stopChatMutation, toast]);

  const logger = useCallback(
    (level: "debug" | "info" | "warn" | "error", message: string, meta?: Record<string, unknown>) => {
      if (import.meta.env?.DEV) {
        // eslint-disable-next-line no-console
        console[level](`[UnifiedAgentChat] ${message}`, meta);
      }
    },
    [],
  );

  const updateSelection = useMutation({
    mutationFn: (selection: SelectedLLMModel) => {
      if (selection.deploymentRef) {
        return saveLLMDeploymentSelection({ deployment_ref: selection.deploymentRef });
      }
      return saveLLMSelection(selection);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/llm/selection"] });
      toast({
        title: "Model updated",
        description: "Your model selection has been saved.",
      });
    },
    onError: (error: Error, selection) => {
      toast({
        title: `Model update failed: ${selection.provider}/${selection.model}`,
        description: error.message,
        variant: "destructive",
      });
    },
  });

  const strategy = useMemo(() => {
    if (featureFlags.enableBasicChat) {
      // Phase 6: indicator reflects the composite (primary + plan)
      // selection. Plan stacks on top of agent / full_access; chat
      // intentionally cannot carry the overlay.
      let indicatorLabel: string;
      if (primaryMode === "chat") {
        indicatorLabel = "Chat";
      } else if (primaryMode === "agent_full") {
        indicatorLabel = planMode ? "Full Access + Plan" : "Agent (Full Access)";
      } else {
        indicatorLabel = planMode ? "Agent + Plan" : "Agent";
      }
      return createBasicChatStrategy({
        sendMessage: interactiveSend,
        inputPlaceholder: "Chat with AI (Enter to send)...",
        indicatorText: indicatorLabel,
      });
    }
    return createModeStrategy({
      type: "interactive",
      sendMessage: interactiveSend,
    });
  }, [interactiveSend, primaryMode, planMode]);

  const orchestrator: ModeOrchestrationContract = useMemo(() => {
    return new InteractiveModeOrchestration({
      sendMessageMutation,
      sseConnection,
      sendMessage: interactiveSend,
      logger,
    });
  }, [logger, sendMessageMutation, sseConnection, interactiveSend]);

  useEffect(() => {
    orchestrator.setStrategy(strategy);
  }, [orchestrator, strategy]);

  const chatProvider = useUnifiedChat({
    taskId: activeTaskId ?? null,
    orchestrator,
    onSendError: (error) => {
      toast({
        title: "Unable to send message",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  // Queue integration: model-agnostic queued send UX
  const queue = useSendQueue({
    taskId: activeTaskId ?? null,
    conversationId: currentConversationId ?? null,
    messages: chatProvider.messages,
    // Immediate sends should preserve optimistic updates via chatProvider
    sendImmediate: async (message: string) => chatProvider.sendMessage(message),
    // Dequeued sends should avoid creating optimistic entries
    sendQueued: async (message: string) => {
      try {
        // Reuse existing orchestrator; skip optimistic updates for dequeued messages
        return orchestrator.orchestrateMessageFlow(message, executionMode, { skipOptimistic: true });
      } catch (error) {
        console.error('[Queue] Error processing queued message:', error);
        throw error;
      }
    },
    maxLength: 4000,
    onError: (err) => {
      toast({ title: "Send failed", description: String(err?.message ?? err), variant: "destructive" });
    },
  });

  const [inputValue, setInputValue] = useState("");

  const handleApproval = useCallback(
    (
      action: "approve" | "edit" | "skip",
      editedParameters?: Record<string, unknown>,
      batchResponse?: BatchApprovalDecisions,
    ) => {
      if (!pendingInterrupt || !activeTaskId) return;
      if (pendingInterrupt.taskId !== activeTaskId) return;
      if (resumingInterruptIdsRef.current.has(pendingInterrupt.interruptId)) return;
      resumingInterruptIdsRef.current.add(pendingInterrupt.interruptId);
      
      // Clear the card immediately - don't wait for resume to complete
      const interruptData = { ...pendingInterrupt };
      setPendingInterrupt(null);
      
      // Fire resume in background - tool execution streams via SSE
      resumeMutation.mutate(
        {
          taskId: interruptData.taskId,
          interruptType: interruptData.interruptType,
          interruptId: interruptData.interruptId,
          graphName: interruptData.graphName,
          response: batchResponse ?? {
            action,
            edited_parameters: action === "edit" ? editedParameters : undefined,
          },
        },
        {
          onSuccess: () => {
            // Refetch interrupt state to verify it's cleared
            refetchInterrupt();
          },
          onError: (error) => {
            resumingInterruptIdsRef.current.delete(interruptData.interruptId);
            setPendingInterrupt(interruptData, { allowDismissedReveal: true });
            const message = error instanceof Error ? error.message : "Failed to resume graph";
            toast({
              title: "Approval failed",
              description: message,
              variant: "destructive",
            });
            // Refetch to restore correct state
            refetchInterrupt();
          },
        }
      );
    },
    [pendingInterrupt, activeTaskId, resumeMutation, toast, setPendingInterrupt, refetchInterrupt],
  );

  const handleBatchApproval = useCallback(
    (batchResponse: BatchApprovalDecisions) => {
      handleApproval("approve", undefined, batchResponse);
    },
    [handleApproval],
  );

  const handleClarifySubmit = useCallback(
    (answers: Record<string, string>) => {
      if (!pendingInterrupt || !activeTaskId) return;
      if (pendingInterrupt.interruptType !== "clarify_request") return;
      if (pendingInterrupt.taskId !== activeTaskId) return;
      if (resumingInterruptIdsRef.current.has(pendingInterrupt.interruptId)) return;
      resumingInterruptIdsRef.current.add(pendingInterrupt.interruptId);

      const interruptData = { ...pendingInterrupt };
      setPendingInterrupt(null);

      resumeMutation.mutate(
        {
          taskId: interruptData.taskId,
          interruptType: interruptData.interruptType,
          interruptId: interruptData.interruptId,
          graphName: interruptData.graphName,
          response: {
            action: "answer",
            answers,
          },
        },
        {
          onSuccess: () => {
            refetchInterrupt();
          },
          onError: (error) => {
            resumingInterruptIdsRef.current.delete(interruptData.interruptId);
            setPendingInterrupt(interruptData, { allowDismissedReveal: true });
            const message = error instanceof Error ? error.message : "Failed to submit clarification";
            toast({
              title: "Clarification failed",
              description: message,
              variant: "destructive",
            });
            refetchInterrupt();
          },
        },
      );
    },
    [activeTaskId, pendingInterrupt, refetchInterrupt, resumeMutation, setPendingInterrupt, toast],
  );

  // Subscribe to the retry-state store so per-message retry lookups in
  // ``resolveRetryStateForMessage`` re-render this component (and the
  // forwarded prop) when the store mutates. Phase 5.3: the store is
  // the single source of truth; do NOT mirror its contents into local
  // state.
  useRetryStateStoreVersion();

  const resolveRetryStateForMessage = useCallback(
    (message: ChatMessage): MessageBubbleRetryState | null => {
      if (!activeTaskId) {
        return null;
      }
      const metadata = message.metadata as Record<string, unknown> | undefined;
      const rawTurnId = typeof metadata?.turn_id === "string" ? metadata.turn_id.trim() : "";
      if (!rawTurnId) {
        return null;
      }
      const entry = getRetryStateForTurn(activeTaskId, rawTurnId);
      if (!entry) {
        return null;
      }
      return {
        taskId: entry.taskId,
        turnId: entry.turnId,
        workflowId: entry.workflowId,
        state: entry.state,
        retryAttempt: entry.retryAttempt,
        retryMaxAttempts: entry.retryMaxAttempts,
        inFlight: entry.inFlight,
      };
    },
    [activeTaskId],
  );

  const handleMessageRetry = useCallback(
    (messageId: string) => {
      if (!activeTaskId || retryMutation.isPending) {
        return;
      }
      const message = chatProvider.messages.find((candidate) => candidate.id === messageId);
      if (!message?.metadata) {
        return;
      }
      if (message.metadata.status !== "error" || message.metadata.retryable !== true) {
        return;
      }

      const turnId =
        typeof message.metadata.turn_id === "string" && message.metadata.turn_id.trim().length > 0
          ? message.metadata.turn_id.trim()
          : typeof message.metadata.id === "string" && message.metadata.id.trim().length > 0
            ? message.metadata.id.trim()
            : "";
      if (!turnId) {
        toast({
          title: "Retry unavailable",
          description: "This failed turn is missing a retry identifier.",
          variant: "destructive",
        });
        return;
      }

      // Phase 5.3 defensive guard: even if the bubble's ``disabled``
      // attribute is bypassed, the click handler must NOT issue
      // another POST while a retry lifecycle is already in flight or
      // settled into a non-retryable terminal state.
      const existingRetryState = getRetryStateForTurn(activeTaskId, turnId);
      if (existingRetryState) {
        if (
          existingRetryState.inFlight ||
          existingRetryState.state === "completed" ||
          existingRetryState.state === "cancelled"
        ) {
          return;
        }
      }

      const graphName =
        typeof message.metadata.graph_name === "string" && message.metadata.graph_name.trim().length > 0
          ? message.metadata.graph_name.trim()
          : undefined;

      // Optimistically pin the entry into ``accepted`` so the CTA
      // disables on the next render even before the mutation resolves.
      // The success handler will overwrite with the canonical
      // identity returned by the backend (incl. workflow_id).
      applyRetryStateUpdate({
        taskId: activeTaskId,
        turnId,
        workflowId: existingRetryState?.workflowId ?? null,
        state: "accepted",
      });

      retryMutation.mutate(
        {
          taskId: activeTaskId,
          turnId,
          retryMode: "checkpoint",
          graphName,
        },
        {
          onSuccess: (data) => {
            applyRetryStateUpdate({
              taskId: data.task_id || activeTaskId,
              turnId: data.turn_id || turnId,
              workflowId: data.workflow_id ?? null,
              state: data.state,
              retryAttempt: data.retry_attempt,
              retryMaxAttempts: data.retry_max_attempts,
            });
          },
          onError: (error) => {
            // Mutation failure leaves the in-flight ``accepted``
            // optimistic write stale; reset it so the user can try
            // again. Stream-event lifecycle (Phase 6) will overwrite
            // this if the backend has actually accepted the claim.
            applyRetryStateUpdate({
              taskId: activeTaskId,
              turnId,
              workflowId: existingRetryState?.workflowId ?? null,
              state: "failed",
              retryAttempt: existingRetryState?.retryAttempt ?? null,
              retryMaxAttempts: existingRetryState?.retryMaxAttempts ?? null,
            });
            const detail = error instanceof Error ? error.message : "Failed to retry graph";
            toast({
              title: "Retry failed",
              description: detail,
              variant: "destructive",
            });
          },
        },
      );
    },
    [activeTaskId, chatProvider.messages, retryMutation, toast],
  );

  useLayoutEffect(() => {
    setInputValue("");
  }, [activeTaskId]);

  const baseModeConfig = useMemo(() => strategy.getModeConfig(), [strategy]);

  const modeConfig: ChatMode = useMemo(() => {
    if (!activeTaskId) {
      return {
        ...baseModeConfig,
        canSendMessages: false,
        inputDisabled: true,
        inputPlaceholder: "Select a task to begin chatting",
      };
    }

    if (!chatBootstrap.isReady) {
      const placeholder =
        chatBootstrap.error ??
        chatBootstrap.statusMessage ??
        (chatBootstrap.isPending ? "Preparing chat..." : "Chat is not ready yet");
      return {
        ...baseModeConfig,
        canSendMessages: false,
        inputDisabled: true,
        inputPlaceholder: placeholder,
      };
    }

    if (!isTaskRunning) {
      return {
        ...baseModeConfig,
        canSendMessages: false,
        inputDisabled: true,
        inputPlaceholder: "Task is not running",
      };
    }

    if (!selectedLLMModel) {
      return {
        ...baseModeConfig,
        canSendMessages: false,
        inputDisabled: true,
        inputPlaceholder: "Select a provider and model before sending",
      };
    }

    if (blockedSelectionStatus) {
      return {
        ...baseModeConfig,
        canSendMessages: false,
        inputDisabled: true,
        inputPlaceholder: blockedSelectionMessage,
      };
    }

    return {
      ...baseModeConfig,
      canSendMessages: baseModeConfig.canSendMessages,
      inputDisabled: baseModeConfig.inputDisabled,
    };
  }, [
    baseModeConfig,
    activeTaskId,
    blockedSelectionMessage,
    blockedSelectionStatus,
    chatBootstrap.error,
    chatBootstrap.isPending,
    chatBootstrap.isReady,
    chatBootstrap.statusMessage,
    isTaskRunning,
    selectedLLMModel,
  ]);

  const toolApprovalInterrupt = useMemo<ToolApprovalInterruptDetail | null>(() => {
    if (isToolApprovalInterruptDetail(pendingInterrupt)) {
      return pendingInterrupt;
    }
    return null;
  }, [pendingInterrupt]);

  const clarifyRequestInterrupt = useMemo<ClarifyRequestInterruptDetail | null>(() => {
    if (!activeTaskId || !pendingInterrupt) return null;
    if (pendingInterrupt.taskId !== activeTaskId) return null;
    if (!isClarifyRequestInterruptDetail(pendingInterrupt)) return null;
    return pendingInterrupt;
  }, [activeTaskId, pendingInterrupt]);

  const emptyState = useMemo(() => {
    if (activeTaskId) {
      return undefined;
    }
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center text-sm text-slate-400">
        <p className="text-base font-semibold text-slate-200">No task selected</p>
        <p className="max-w-sm text-xs text-slate-500">
          Choose a task to view reasoning history and interact with the agent.
        </p>
      </div>
    );
  }, [activeTaskId]);

  const handleTaskSelection = useCallback(
    (taskIdentifier: number) => {
      if (Number.isNaN(taskIdentifier)) return;
      if (taskId == null) {
        setActiveChatTaskId(taskIdentifier);
      }
      onTaskChange?.(taskIdentifier);
    },
    [taskId, onTaskChange],
  );

  const handleModelSelection = useCallback(
    (
      selection: SelectedLLMModel,
      options?: { reasoningEffort?: ReasoningEffort },
    ) => {
      if (options?.reasoningEffort) {
        setSelectedReasoningEffort(options.reasoningEffort);
      }
      if (!selection.model || hasSameLLMSelectionIdentity(selection, selectedLLMModel)) {
        return;
      }
      setSelectedLLMModel(selection);
      updateSelection.mutate(selection);
    },
    [updateSelection, selectedLLMModel],
  );

  const handleDownloadTranscript = useCallback(async () => {
    if (!activeTaskId || isTranscriptExporting) {
      return;
    }
    setIsTranscriptExporting(true);
    try {
      const payload = await buildChatTranscriptExport({
        taskId: activeTaskId,
        taskName: activeTask?.name ?? null,
        conversationId: currentConversationId,
      });
      if (payload.messageCount === 0) {
        toast({
          title: "No transcript available",
          description: "There are no user or assistant messages to export.",
        });
        return;
      }
      downloadTextFile(
        payload.markdown,
        payload.filename,
        "text/markdown;charset=utf-8",
      );
      toast({
        title: "Transcript downloaded",
        description: `Exported ${payload.messageCount} messages.`,
      });
    } catch (error) {
      toast({
        title: "Transcript export failed",
        description: error instanceof Error ? error.message : "Could not export transcript.",
        variant: "destructive",
      });
    } finally {
      setIsTranscriptExporting(false);
    }
  }, [
    activeTask?.name,
    activeTaskId,
    currentConversationId,
    isTranscriptExporting,
    toast,
  ]);

  const header = headerSlot ?? (
    <TaskModelSelectors
      selectedTaskId={activeTaskId ?? null}
      conversationId={currentConversationId}
      onTaskChange={handleTaskSelection}
      selectedSelection={selectedLLMModel}
      selectionStatus={blockedSelectionStatus}
      onModelChange={handleModelSelection}
      selectedReasoningEffort={selectedReasoningEffort}
      onReasoningEffortChange={setSelectedReasoningEffort}
      tasks={tasks}
      llmCatalog={llmCatalog}
      isConnected={chatProvider.isConnected}
      runStates={runStates}
      onDownloadTranscript={handleDownloadTranscript}
      isTranscriptDownloadPending={isTranscriptExporting}
    />
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-slate-950">
      {header}
      <MessageList
        messages={chatProvider.messages}
        taskId={activeTaskId}
        isLoading={chatProvider.isLoading || chatBootstrap.isPending}
        isConnected={chatProvider.isConnected}
        hasMore={chatProvider.hasMore}
        onLoadMore={chatProvider.loadMore}
        onMessageRetry={handleMessageRetry}
        resolveRetryState={resolveRetryStateForMessage}
        emptyState={emptyState}
      />

      {toolApprovalInterrupt && (
        <div className="px-4 pb-2">
          <ToolApprovalCard
            payload={toolApprovalInterrupt.payload}
            onApprove={() => handleApproval("approve")}
            onEdit={(params) => handleApproval("edit", params)}
            onSkip={() => handleApproval("skip")}
            onBatchSubmit={handleBatchApproval}
            isSubmitting={resumeMutation.isPending}
          />
        </div>
      )}

      {clarifyRequestInterrupt && (
        <div className="px-4 pb-2">
          <ClarifyRequiredCard
            payload={clarifyRequestInterrupt.payload}
            onSubmit={handleClarifySubmit}
            isSubmitting={resumeMutation.isPending}
          />
        </div>
      )}

      {/* PlanCard for plan_review interrupts - rendered as fixed position overlay */}
      {activeTaskId && (
        <PlanCard
          taskId={activeTaskId}
          interruptState={{
            interrupt: pendingInterrupt,
            refetch: refetchInterrupt,
            setInterrupt: setPendingInterrupt,
          }}
        />
      )}

      {featureFlags.enableSendQueueUI && (
        <div className="flex items-center justify-end px-4 pb-1">
          <QueueIndicator queue={queue} />
        </div>
      )}
      {/* Removed console.log to prevent excessive re-renders */}

      <ChatInput
        value={inputValue}
        onChange={setInputValue}
        onStop={handleStopChat}
        onSend={async (message) => {
          if (isContextCompacting) return;
          const pendingMessage = message;
          setInputValue("");
          try {
            if (featureFlags.enableSendQueueUI) {
              await queue.onUserSend(pendingMessage);
            } else {
              await chatProvider.sendMessage(pendingMessage);
            }
          } catch {
            // Error already reported via hook callback.
            setInputValue((current) => (current ? current : pendingMessage));
          }
        }}
        mode={modeConfig}
        disabled={
          !activeTaskId ||
          sendMessageMutation.isPending ||
          !chatBootstrap.isReady ||
          isChatRunStopping ||
          !selectedLLMModel ||
          Boolean(blockedSelectionStatus)
        }
        submissionDisabled={isContextCompacting}
        statusMessage={
          isContextCompacting ? "Compacting conversation context…" : undefined
        }
        maxLength={4000}
        isSending={sendMessageMutation.isPending}
        isRunning={isChatGenerationActive}
        isStopping={Boolean(isChatGenerationActive && (stopChatMutation.isPending || isChatRunStopping))}
        autoFocus={true}
        primaryMode={primaryMode}
        planMode={planMode}
        onPrimaryModeChange={onChatModeChange ? handlePrimaryModeChange : undefined}
        onPlanModeChange={onChatModeChange ? handlePlanToggle : undefined}
      />
    </div>
  );
}

export default UnifiedAgentChat;
