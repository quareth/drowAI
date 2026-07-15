/**
 * Thin integration hook for multiplex runtime streaming services.
 *
 * Responsibilities:
 * - lifecycle wiring for RuntimeStreamClient
 * - derive desired subscriptions with TaskSubscriptionPlanner
 * - ingest full agent_reasoning envelopes via StreamPacketIngestor
 * - broadcast legacy runtime status events for existing listeners
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { wsConfig } from "@/utils/websocket-config";
import { StreamPacketIngestor } from "@/services/runtime_stream/StreamPacketIngestor";
import { RuntimeStreamClient } from "@/services/runtime_stream/RuntimeStreamClient";
import {
  getAccessToken,
  recoverSessionAfterAuthFailure,
} from "@/lib/auth-session";
import {
  resetTaskStreamForResync,
  setChatReadyState,
  setConnectionState,
} from "@/state/chat-stream-store";
import {
  applyContextCompactionLifecycleEvent,
  releaseContextCompactionGatesForTask,
} from "@/state/context-window-store";
import {
  applyRetryStateUpdate,
  readRetryLifecycleState,
  type RetryLifecycleState,
} from "@/state/retry-state-store";
import {
  computeDesiredTaskSubscriptions,
  planSubscriptionActions,
} from "@/services/runtime_stream/TaskSubscriptionPlanner";
import type { RuntimeAgentReasoningEnvelope } from "@/services/runtime_stream/types";
import type { RuntimeStreamConnectionStatus } from "@/services/runtime_stream/RuntimeStreamClient";
import type { RuntimeTaskSubscriptionState } from "@/services/runtime_stream/types";
import { onActiveTenantChanged } from "@/lib/tenant-context";

interface UseMultiTaskStreamManagerOptions {
  taskIds: number[];
  enabled: boolean;
}

function normalizeTaskIds(taskIds: number[]): number[] {
  return Array.from(
    new Set(taskIds.filter((value) => Number.isFinite(value) && value > 0)),
  ).sort((a, b) => a - b);
}

function applyConnectionStatus(
  taskIds: number[],
  status: RuntimeStreamConnectionStatus,
): void {
  const normalizedTaskIds = normalizeTaskIds(taskIds);
  for (const taskId of normalizedTaskIds) {
    if (status.phase === "closed" || status.phase === "idle") {
      releaseContextCompactionGatesForTask(taskId);
    }
    if (status.phase === "connecting") {
      setConnectionState(taskId, {
        isConnected: false,
        isConnecting: true,
        connectionError: null,
      });
      continue;
    }
    if (status.phase === "closed") {
      setConnectionState(taskId, {
        isConnected: false,
        isConnecting: false,
        connectionError: status.error ?? null,
      });
      continue;
    }
    setConnectionState(taskId, {
      isConnected: false,
      isConnecting: false,
      connectionError: null,
    });
  }
}

function resolveTaskConnectionProjection(
  taskId: number,
  desiredTaskIds: Set<number>,
  socketStatus: RuntimeStreamConnectionStatus,
  subscriptionStates: Map<number, RuntimeTaskSubscriptionState>,
): {
  isConnected: boolean;
  isConnecting: boolean;
  connectionError: string | null;
} {
  if (!desiredTaskIds.has(taskId)) {
    return { isConnected: false, isConnecting: false, connectionError: null };
  }

  const subscriptionState = subscriptionStates.get(taskId);
  const subscriptionError =
    subscriptionState?.phase === "error" && subscriptionState.errorReason
      ? subscriptionState.errorReason
      : null;

  if (socketStatus.phase === "open") {
    if (subscriptionState?.phase === "subscribed") {
      return { isConnected: true, isConnecting: false, connectionError: null };
    }
    if (subscriptionError) {
      return {
        isConnected: false,
        isConnecting: false,
        connectionError: subscriptionError,
      };
    }
    return { isConnected: false, isConnecting: true, connectionError: null };
  }

  if (socketStatus.phase === "connecting") {
    return { isConnected: false, isConnecting: true, connectionError: null };
  }

  if (socketStatus.phase === "closed") {
    return {
      isConnected: false,
      isConnecting: false,
      connectionError: socketStatus.error ?? subscriptionError ?? null,
    };
  }

  return {
    isConnected: false,
    isConnecting: false,
    connectionError: subscriptionError,
  };
}

function applyProjectedConnectionStatus(
  taskIds: number[],
  desiredTaskIds: number[],
  socketStatus: RuntimeStreamConnectionStatus,
  subscriptionStates: Map<number, RuntimeTaskSubscriptionState>,
): void {
  const normalizedTaskIds = normalizeTaskIds(taskIds);
  const desiredSet = new Set(normalizeTaskIds(desiredTaskIds));

  for (const taskId of normalizedTaskIds) {
    if (socketStatus.phase === "closed") {
      releaseContextCompactionGatesForTask(taskId);
    }
    setConnectionState(
      taskId,
      resolveTaskConnectionProjection(
        taskId,
        desiredSet,
        socketStatus,
        subscriptionStates,
      ),
    );
  }
}

function readOptionalNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return null;
}

function readOptionalString(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * Per-task last-applied resync sequence. Used to keep
 * ``resetTaskStreamForResync`` idempotent across duplicate retry events:
 * once a resync at sequence N has fired for task T, a later event for T
 * with sequence <= N is a no-op so the cursor never goes backwards and
 * the local store is not repeatedly cleared by stream-event reordering.
 */
const lastResyncSequenceByTask = new Map<number, number>();

function shouldTriggerResync(
  _state: RetryLifecycleState | null,
  metadata: Record<string, unknown>,
): boolean {
  return Boolean(metadata.transcript_resync_required);
}

interface CheckpointOperationStateDetail {
  turnId: string | null;
  lifecycleState: RetryLifecycleState | null;
  workflowId: number | null;
  retryAttempt: number | null;
  retryMaxAttempts: number | null;
  checkpointId: string | null;
  retryMode: string | null;
  graphName: string | null;
  operationKind: string | null;
  transcriptResyncRequired: boolean;
}

function readCheckpointOperationStateDetail(
  metadata: Record<string, unknown>,
): CheckpointOperationStateDetail {
  return {
    turnId: readOptionalString(metadata.turn_id),
    lifecycleState: readRetryLifecycleState(metadata.state),
    workflowId: readOptionalNumber(metadata.workflow_id),
    retryAttempt: readOptionalNumber(metadata.retry_attempt),
    retryMaxAttempts: readOptionalNumber(metadata.retry_max_attempts),
    checkpointId: readOptionalString(metadata.checkpoint_id),
    retryMode: readOptionalString(metadata.retry_mode),
    graphName: readOptionalString(metadata.graph_name),
    operationKind: readOptionalString(metadata.operation_kind),
    transcriptResyncRequired: Boolean(metadata.transcript_resync_required),
  };
}

function applyRetryStateFromDetail(
  taskId: number,
  detail: CheckpointOperationStateDetail,
): void {
  if (detail.turnId && detail.lifecycleState) {
    applyRetryStateUpdate({
      taskId,
      turnId: detail.turnId,
      workflowId: detail.workflowId,
      state: detail.lifecycleState,
      retryAttempt: detail.retryAttempt,
      retryMaxAttempts: detail.retryMaxAttempts,
    });
  }
}

function dispatchRetryCompatibilityEvent(
  taskId: number,
  detail: CheckpointOperationStateDetail,
  fallbackSequence?: number,
): void {
  // Fire a window-level compatibility event so non-React listeners (e.g.
  // diagnostics, future surfaces) can observe the canonical retry
  // lifecycle without having to subscribe to the store.
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("task-retry-state", {
        detail: {
          taskId,
          turnId: detail.turnId,
          workflowId: detail.workflowId,
          state: detail.lifecycleState,
          retryAttempt: detail.retryAttempt,
          retryMaxAttempts: detail.retryMaxAttempts,
          checkpointId: detail.checkpointId,
          retryMode: detail.retryMode,
          graphName: detail.graphName,
          transcriptResyncRequired: detail.transcriptResyncRequired,
          sequence: fallbackSequence,
        },
      }),
    );
  }
}

function applyTranscriptResyncIfNeeded(
  taskId: number,
  metadata: Record<string, unknown>,
  lifecycleState: RetryLifecycleState | null,
  fallbackSequence?: number,
): void {
  if (shouldTriggerResync(lifecycleState, metadata)) {
    const sequenceCandidate =
      typeof fallbackSequence === "number" && Number.isFinite(fallbackSequence)
        ? Math.floor(fallbackSequence)
        : undefined;
    const lastApplied = lastResyncSequenceByTask.get(taskId);
    if (
      sequenceCandidate !== undefined &&
      typeof lastApplied === "number" &&
      sequenceCandidate <= lastApplied
    ) {
      return;
    }
    if (sequenceCandidate !== undefined) {
      lastResyncSequenceByTask.set(taskId, sequenceCandidate);
    }
    resetTaskStreamForResync(taskId, sequenceCandidate);
  }
}

function handleRetryStateEvent(
  taskId: number,
  metadata: Record<string, unknown>,
  fallbackSequence?: number,
): void {
  const detail = readCheckpointOperationStateDetail(metadata);
  applyRetryStateFromDetail(taskId, detail);
  dispatchRetryCompatibilityEvent(taskId, detail, fallbackSequence);
  applyTranscriptResyncIfNeeded(
    taskId,
    metadata,
    detail.lifecycleState,
    fallbackSequence,
  );
}

function handleCheckpointRewindStateEvent(
  taskId: number,
  metadata: Record<string, unknown>,
  fallbackSequence?: number,
): void {
  const detail = readCheckpointOperationStateDetail(metadata);

  if (detail.operationKind === "retry") {
    applyRetryStateFromDetail(taskId, detail);
    dispatchRetryCompatibilityEvent(taskId, detail, fallbackSequence);
  }

  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("task-checkpoint-rewind-state", {
        detail: {
          taskId,
          operationKind: detail.operationKind,
          turnId: detail.turnId,
          workflowId: detail.workflowId,
          state:
            typeof metadata.state === "string"
              ? metadata.state.trim().toLowerCase()
              : null,
          checkpointId: detail.checkpointId,
          graphName: detail.graphName,
          transcriptResyncRequired: detail.transcriptResyncRequired,
          sequence: fallbackSequence,
        },
      }),
    );
  }

  applyTranscriptResyncIfNeeded(
    taskId,
    metadata,
    detail.lifecycleState,
    fallbackSequence,
  );
}

/**
 * Test-only hook for clearing the per-task resync de-duplication map.
 * Not exported as part of the runtime contract.
 */
export function __resetMultiTaskStreamManagerStateForTest(): void {
  lastResyncSequenceByTask.clear();
}

function dispatchRuntimeCompatibilityEvent(
  taskId: number,
  packet: Record<string, unknown>,
  fallbackSequence?: number,
): void {
  if (typeof window === "undefined" || !packet || typeof packet !== "object") {
    return;
  }
  const obj =
    packet.obj && typeof packet.obj === "object"
      ? (packet.obj as Record<string, unknown>)
      : packet;
  if (!obj || typeof obj !== "object") {
    return;
  }
  const type = typeof obj.type === "string" ? obj.type : "";
  const content = typeof obj.content === "string" ? obj.content : "";
  if (!type && !content) return;

  if (type === "graph_interrupt") {
    const payload = obj.payload as Record<string, unknown> | undefined;
    const payloadInterruptId =
      typeof payload?.interrupt_id === "string" ? payload.interrupt_id : null;
    const interruptId =
      (typeof obj.interrupt_id === "string" && obj.interrupt_id) ||
      payloadInterruptId ||
      `${obj.graph_name}:task:${obj.task_id}:legacy`;
    window.dispatchEvent(
      new CustomEvent("graph-interrupt", {
        detail: {
          taskId: typeof obj.task_id === "number" ? obj.task_id : taskId,
          threadId:
            typeof obj.thread_id === "string"
              ? obj.thread_id
              : `task-${taskId}`,
          interruptId,
          checkpointId:
            typeof obj.checkpoint_id === "string" ? obj.checkpoint_id : null,
          interruptType:
            typeof obj.interrupt_type === "string"
              ? obj.interrupt_type
              : "tool_approval",
          graphName:
            typeof obj.graph_name === "string" ? obj.graph_name : "simple_tool",
          payload: obj.payload,
        },
      }),
    );
    return;
  }

  const metadata = (obj.metadata ?? {}) as Record<string, unknown>;
  const statusTaskId =
    typeof metadata.task_id === "number"
      ? metadata.task_id
      : typeof obj.task_id === "number"
        ? (obj.task_id as number)
        : typeof packet.task_id === "number"
          ? (packet.task_id as number)
          : taskId;

  const eventKind =
    content === "streaming_state" || type === "streaming_state"
      ? "streaming_state"
      : content === "run_state" || type === "run_state"
        ? "run_state"
        : content === "interrupt_state" || type === "interrupt_state"
          ? "interrupt_state"
          : content === "retry_state" || type === "retry_state"
            ? "retry_state"
            : content === "checkpoint_rewind_state" ||
                type === "checkpoint_rewind_state"
              ? "checkpoint_rewind_state"
              : content === "chat_ready" || type === "chat_ready"
                ? "chat_ready"
              : content === "context_window" || type === "context_window"
                ? "context_window"
                : content === "task_notification" || type === "task_notification"
                  ? "task_notification"
                  : type === "plan_created"
                    ? "plan_created"
                    : type === "todo_progress"
                      ? "todo_progress"
                      : null;

  const normalizeTodoStatus = (
    value: unknown,
  ): "pending" | "in_progress" | "completed" | "skipped" => {
    const normalized =
      typeof value === "string" ? value.trim().toLowerCase() : "";
    if (normalized === "in_progress") return "in_progress";
    if (normalized === "completed") return "completed";
    if (normalized === "skipped") return "skipped";
    return "pending";
  };

  if (eventKind === "streaming_state") {
    window.dispatchEvent(
      new CustomEvent("llm-streaming", {
        detail: {
          taskId: statusTaskId,
          isStreaming: Boolean(metadata.is_streaming ?? metadata.isStreaming),
          queuedCount:
            typeof metadata.queued_count === "number"
              ? metadata.queued_count
              : undefined,
          sequence: fallbackSequence,
        },
      }),
    );
    return;
  }
  if (eventKind === "run_state") {
    window.dispatchEvent(
      new CustomEvent("task-run-state", {
        detail: {
          taskId: statusTaskId,
          state:
            typeof metadata.state === "string" ? metadata.state : "unknown",
          turnId:
            typeof metadata.turn_id === "string" ? metadata.turn_id : null,
          cancelRequested: Boolean(metadata.cancel_requested),
          cancelReason:
            typeof metadata.cancel_reason === "string"
              ? metadata.cancel_reason
              : null,
        },
      }),
    );
    return;
  }
  if (eventKind === "interrupt_state") {
    window.dispatchEvent(
      new CustomEvent("task-interrupt-state", {
        detail: {
          taskId: statusTaskId,
          interruptId:
            typeof metadata.interrupt_id === "string"
              ? metadata.interrupt_id
              : null,
          interruptType:
            typeof metadata.interrupt_type === "string"
              ? metadata.interrupt_type
              : null,
          graphName:
            typeof metadata.graph_name === "string"
              ? metadata.graph_name
              : null,
          threadId:
            typeof metadata.thread_id === "string" ? metadata.thread_id : null,
          turnId:
            typeof metadata.turn_id === "string" ? metadata.turn_id : null,
          turnSequence:
            typeof metadata.turn_sequence === "number"
              ? metadata.turn_sequence
              : null,
          checkpointId:
            typeof metadata.checkpoint_id === "string"
              ? metadata.checkpoint_id
              : null,
          state:
            typeof metadata.state === "string" ? metadata.state : "unknown",
          updatedAt:
            typeof metadata.updated_at === "string"
              ? metadata.updated_at
              : null,
          createdAt:
            typeof metadata.created_at === "string"
              ? metadata.created_at
              : null,
        },
      }),
    );
    return;
  }
  if (eventKind === "retry_state") {
    handleRetryStateEvent(statusTaskId, metadata, fallbackSequence);
    return;
  }
  if (eventKind === "checkpoint_rewind_state") {
    handleCheckpointRewindStateEvent(statusTaskId, metadata, fallbackSequence);
    return;
  }
  if (eventKind === "chat_ready") {
    setChatReadyState(statusTaskId, Boolean(metadata.chat_ready), metadata);
    return;
  }
  if (eventKind === "context_window") {
    applyContextCompactionLifecycleEvent(metadata, fallbackSequence);
    window.dispatchEvent(
      new CustomEvent("context-window-state", {
        detail: {
          taskId: statusTaskId,
          metadata,
          sequence: fallbackSequence,
        },
      }),
    );
    return;
  }
  if (eventKind === "task_notification") {
    const readNumber = (key: string): number | null => {
      const value = metadata[key];
      return typeof value === "number" && Number.isFinite(value) ? value : null;
    };
    const readString = (key: string): string | null => {
      const value = metadata[key];
      return typeof value === "string" && value.trim() ? value.trim() : null;
    };
    window.dispatchEvent(
      new CustomEvent("task-notification", {
        detail: {
          taskId: statusTaskId,
          category: readString("category") ?? "task",
          title: readString("title") ?? "Task notification",
          body: readString("body") ?? "",
          createdAt: readString("created_at") ?? readString("createdAt") ?? new Date().toISOString(),
          sequence: fallbackSequence,
          metadata: {
            ...metadata,
            engagementId: readNumber("engagement_id"),
            ingestionRunId: readString("ingestion_run_id"),
            sourceExecutionId: readString("source_execution_id"),
            toolName: readString("tool_name"),
            assetCount: readNumber("asset_insert_count") ?? 0,
            findingCount: readNumber("finding_insert_count") ?? 0,
          },
        },
      }),
    );
    return;
  }
  if (eventKind === "plan_created") {
    const rawPlanSteps = Array.isArray(obj.plan_steps) ? obj.plan_steps : [];
    const planSteps = rawPlanSteps
      .map((step) => (typeof step === "string" ? step.trim() : ""))
      .filter((step) => step.length > 0);
    const rawTodoList = Array.isArray(obj.todo_list) ? obj.todo_list : [];
    const todoList = rawTodoList.map((todo, index) => {
      const todoObject =
        todo && typeof todo === "object"
          ? (todo as Record<string, unknown>)
          : {};
      const fallbackText = planSteps[index] ?? `Step ${index + 1}`;
      const text =
        typeof todoObject.text === "string" && todoObject.text.trim().length > 0
          ? todoObject.text
          : fallbackText;
      const id =
        typeof todoObject.id === "string" && todoObject.id.trim().length > 0
          ? todoObject.id
          : `${index + 1}`;
      return {
        id,
        text,
        status: normalizeTodoStatus(todoObject.status),
      };
    });

    window.dispatchEvent(
      new CustomEvent("task-plan-created", {
        detail: {
          taskId: statusTaskId,
          goal: typeof obj.goal === "string" ? obj.goal : "",
          planSteps,
          todoList,
          runId:
            typeof obj.run_id === "number" && Number.isFinite(obj.run_id)
              ? Math.floor(obj.run_id)
              : undefined,
          planVersion:
            typeof obj.plan_version === "number" &&
            Number.isFinite(obj.plan_version)
              ? Math.floor(obj.plan_version)
              : undefined,
          sequence: fallbackSequence,
        },
      }),
    );
    return;
  }
  if (eventKind === "todo_progress") {
    const rawUpdates = Array.isArray(obj.todo_updates) ? obj.todo_updates : [];
    const eventPlanVersion =
      typeof obj.plan_version === "number" && Number.isFinite(obj.plan_version)
        ? Math.floor(obj.plan_version)
        : undefined;
    const updates: Array<{
      id: string;
      text?: string;
      index?: number;
      status: "pending" | "in_progress" | "completed" | "skipped";
      plan_version?: number;
    }> = [];
    for (const rawUpdate of rawUpdates) {
      if (!rawUpdate || typeof rawUpdate !== "object") {
        continue;
      }
      const updateObject = rawUpdate as Record<string, unknown>;
      updates.push({
        id: typeof updateObject.id === "string" ? updateObject.id : "",
        text:
          typeof updateObject.text === "string" ? updateObject.text : undefined,
        index:
          typeof updateObject.index === "number" &&
          Number.isFinite(updateObject.index)
            ? Math.floor(updateObject.index)
            : undefined,
        status: normalizeTodoStatus(updateObject.status),
        plan_version:
          typeof updateObject.plan_version === "number" &&
          Number.isFinite(updateObject.plan_version)
            ? Math.floor(updateObject.plan_version)
            : eventPlanVersion,
      });
    }
    if (updates.length === 0) {
      return;
    }

    window.dispatchEvent(
      new CustomEvent("task-todo-progress", {
        detail: {
          taskId: statusTaskId,
          updates,
          runId:
            typeof obj.run_id === "number" && Number.isFinite(obj.run_id)
              ? Math.floor(obj.run_id)
              : undefined,
          planVersion: eventPlanVersion,
          sequence: fallbackSequence,
        },
      }),
    );
  }
}

export function useMultiTaskStreamManager({
  taskIds,
  enabled,
}: UseMultiTaskStreamManagerOptions): void {
  const [tenantSwitchEpoch, setTenantSwitchEpoch] = useState(0);
  const desiredTaskIds = useMemo(
    () => computeDesiredTaskSubscriptions({ runningTaskIds: taskIds }),
    [taskIds],
  );

  const clientRef = useRef<RuntimeStreamClient | null>(null);
  const ingestorRef = useRef<StreamPacketIngestor>(new StreamPacketIngestor());
  const appliedTaskIdsRef = useRef<number[]>([]);
  const desiredTaskIdsRef = useRef<number[]>([]);
  const connectionStatusRef = useRef<RuntimeStreamConnectionStatus>({
    phase: "idle",
    error: null,
  });
  const subscriptionStatesRef = useRef<
    Map<number, RuntimeTaskSubscriptionState>
  >(new Map());
  const appliedTenantEpochRef = useRef(0);

  useEffect(() => {
    return onActiveTenantChanged(() => {
      setTenantSwitchEpoch((current) => current + 1);
    });
  }, []);

  useEffect(() => {
    desiredTaskIdsRef.current = desiredTaskIds;
  }, [desiredTaskIds]);

  useEffect(() => {
    return () => {
      const staleTaskIds = normalizeTaskIds([
        ...appliedTaskIdsRef.current,
        ...desiredTaskIdsRef.current,
      ]);
      applyConnectionStatus(staleTaskIds, { phase: "idle", error: null });
      clientRef.current?.disconnect();
      clientRef.current = null;
      appliedTaskIdsRef.current = [];
      desiredTaskIdsRef.current = [];
      connectionStatusRef.current = { phase: "idle", error: null };
      subscriptionStatesRef.current.clear();
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    if (!enabled) {
      const staleTaskIds = normalizeTaskIds([
        ...appliedTaskIdsRef.current,
        ...desiredTaskIdsRef.current,
      ]);
      applyConnectionStatus(staleTaskIds, { phase: "idle", error: null });
      clientRef.current?.disconnect();
      clientRef.current = null;
      appliedTaskIdsRef.current = [];
      desiredTaskIdsRef.current = [];
      connectionStatusRef.current = { phase: "idle", error: null };
      subscriptionStatesRef.current.clear();
      return;
    }

    if (
      clientRef.current &&
      appliedTenantEpochRef.current !== tenantSwitchEpoch
    ) {
      clientRef.current.disconnect();
      clientRef.current = null;
      appliedTaskIdsRef.current = [];
      subscriptionStatesRef.current.clear();
    }
    appliedTenantEpochRef.current = tenantSwitchEpoch;

    if (!clientRef.current) {
      const wsUrl = wsConfig.getWebSocketUrl("/ws", { type: "agent-multi" });
      clientRef.current = new RuntimeStreamClient({
        url: wsUrl,
        tokenProvider: () => getAccessToken(),
        onAuthenticationFailure: async (reason) => {
          const recovered = await recoverSessionAfterAuthFailure({
            source: "runtime_ws",
            reason,
          });
          if (recovered) {
            clientRef.current?.connect();
            clientRef.current?.setDesiredTaskIds(desiredTaskIdsRef.current);
          }
        },
        onServerMessage: (message) => {
          if (message.type !== "agent_reasoning") return;
          const envelope = message as RuntimeAgentReasoningEnvelope;
          const ingested = ingestorRef.current.ingestEnvelope(envelope);
          if (
            !ingested ||
            !envelope.packet ||
            typeof envelope.packet !== "object"
          ) {
            return;
          }
          dispatchRuntimeCompatibilityEvent(
            envelope.taskId,
            envelope.packet as Record<string, unknown>,
            envelope.sequence,
          );
        },
        onSubscriptionStateChange: (taskId, state) => {
          subscriptionStatesRef.current.set(taskId, state);
          applyProjectedConnectionStatus(
            desiredTaskIdsRef.current,
            desiredTaskIdsRef.current,
            connectionStatusRef.current,
            subscriptionStatesRef.current,
          );
        },
        onConnectionStatusChange: (status) => {
          const previousStatus = connectionStatusRef.current;
          const connectionDropped =
            status.phase === "closed" ||
            (previousStatus.phase === "open" && status.phase === "connecting");
          if (connectionDropped) {
            for (const taskId of desiredTaskIdsRef.current) {
              releaseContextCompactionGatesForTask(taskId);
            }
          }
          connectionStatusRef.current = status;
          applyProjectedConnectionStatus(
            desiredTaskIdsRef.current,
            desiredTaskIdsRef.current,
            status,
            subscriptionStatesRef.current,
          );
        },
      });
      clientRef.current.connect();
    }

    const actions = planSubscriptionActions(
      appliedTaskIdsRef.current,
      desiredTaskIds,
    );
    if (actions.length > 0) {
      const removedTaskIds = appliedTaskIdsRef.current.filter(
        (taskId) => !desiredTaskIds.includes(taskId),
      );
      if (removedTaskIds.length > 0) {
        applyConnectionStatus(removedTaskIds, { phase: "idle", error: null });
        for (const taskId of removedTaskIds) {
          subscriptionStatesRef.current.delete(taskId);
        }
      }
      clientRef.current.setDesiredTaskIds(desiredTaskIds);
      appliedTaskIdsRef.current = [...desiredTaskIds];
    } else if (
      appliedTaskIdsRef.current.length === 0 &&
      desiredTaskIds.length === 0
    ) {
      clientRef.current.setDesiredTaskIds([]);
    }
    for (const taskId of desiredTaskIds) {
      subscriptionStatesRef.current.set(
        taskId,
        clientRef.current.getTaskSubscriptionState(taskId),
      );
    }
    const connectionStatus = clientRef.current.getConnectionStatus();
    connectionStatusRef.current = connectionStatus;
    applyProjectedConnectionStatus(
      desiredTaskIds,
      desiredTaskIds,
      connectionStatus,
      subscriptionStatesRef.current,
    );
  }, [desiredTaskIds, enabled, tenantSwitchEpoch]);
}

export default useMultiTaskStreamManager;
