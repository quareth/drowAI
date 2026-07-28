/**
 * React hooks for reading subagent-run store state.
 *
 * Responsibilities:
 * - expose task-scoped run snapshots without mutating stream state
 * - derive conversation, parent-run, and selected-run views for drawer components
 * - keep drawer presentation selectors separate from lifecycle hydration
 */
import { useEffect, useMemo } from "react";

import { apiFetch } from "@/lib/api-config";
import {
  getAgentRunParentGroupingKey,
  getAgentRun,
  reconcileAgentRunsWithLocalStatus,
  useAgentRunStoreSnapshot,
  type AgentRunRecord,
} from "../state/agent-stream-store";
import type {
  AgentRunPresentationState,
  AgentRunStatus,
  LocalAgentRunListResponse,
  LocalAgentRunStatusProjection,
} from "../contracts/agent-run";

export function useAgentRuns(taskId: number | null | undefined): AgentRunRecord[] {
  return useAgentRunStoreSnapshot(taskId).runs;
}

export function useAgentRunsForConversation(
  taskId: number | null | undefined,
  anchorParentRunId: string | null | undefined,
): AgentRunRecord[] {
  const snapshot = useAgentRunStoreSnapshot(taskId);
  return useMemo(() => {
    const normalizedAnchor =
      typeof anchorParentRunId === "string" ? anchorParentRunId.trim() : "";
    if (!normalizedAnchor) {
      return [];
    }
    const anchorRun = snapshot.runs.find(
      run => getAgentRunParentGroupingKey(run) === normalizedAnchor,
    );
    if (!anchorRun) {
      return [];
    }
    const conversationId = anchorRun.conversationId.trim();
    if (!conversationId) {
      return snapshot.runs.filter(
        run => getAgentRunParentGroupingKey(run) === normalizedAnchor,
      );
    }
    return snapshot.runs.filter(run => run.conversationId === conversationId);
  }, [anchorParentRunId, snapshot.runs]);
}

export function useAgentRun(
  taskId: number | null | undefined,
  agentRunId: string | null | undefined,
): AgentRunRecord | null {
  const snapshot = useAgentRunStoreSnapshot(taskId);
  const normalizedAgentRunId = typeof agentRunId === "string" ? agentRunId.trim() : "";
  if (!normalizedAgentRunId) {
    return null;
  }
  return snapshot.runsById[normalizedAgentRunId] ?? null;
}

export function useSelectedAgentRun(
  taskId: number | null | undefined,
): AgentRunRecord | null {
  const snapshot = useAgentRunStoreSnapshot(taskId);
  const selectedAgentRunId = snapshot.presentation.selectedAgentRunId;
  if (!selectedAgentRunId) {
    return null;
  }
  return snapshot.runsById[selectedAgentRunId] ?? null;
}

export function useAgentRunPresentation(
  taskId: number | null | undefined,
): AgentRunPresentationState {
  return useAgentRunStoreSnapshot(taskId).presentation;
}

export function useAgentRunLocalStatusHydration(
  taskId: number | null | undefined,
): void {
  const snapshot = useAgentRunStoreSnapshot(taskId);
  const activeRunKey = useMemo(() => {
    if (!isValidTaskId(taskId)) {
      return "";
    }
    return snapshot.runs
      .filter(run => !isTerminalStatus(run.status))
      .map(run => run.agentRunId)
      .sort()
      .join(",");
  }, [snapshot.runs, taskId]);

  useEffect(() => {
    if (!isValidTaskId(taskId) || !activeRunKey) {
      return;
    }

    const scopedTaskId = taskId;
    const controller = new AbortController();
    let cancelled = false;

    async function hydrateLocalStatus() {
      const response = await apiFetch(`/api/tasks/${scopedTaskId}/agent-runs/local`, {
        method: "GET",
        signal: controller.signal,
      });
      if (!response.ok || cancelled) {
        return;
      }
      const payload = await response.json();
      const localRuns = readLocalAgentRuns(payload, scopedTaskId);
      if (cancelled || localRuns === null) {
        return;
      }
      reconcileAgentRunsWithLocalStatus(scopedTaskId, localRuns);
    }

    void hydrateLocalStatus().catch(error => {
      if (!cancelled && !(error instanceof DOMException && error.name === "AbortError")) {
        // Local status hydration is best-effort; stream replay remains authoritative for visible data.
      }
    });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [activeRunKey, taskId]);
}

export function readAgentRun(
  taskId: number | null | undefined,
  agentRunId: string | null | undefined,
): AgentRunRecord | null {
  return getAgentRun(taskId, agentRunId);
}

function isValidTaskId(taskId: number | null | undefined): taskId is number {
  return typeof taskId === "number" && Number.isFinite(taskId) && taskId > 0;
}

function isTerminalStatus(status: AgentRunStatus): boolean {
  return (
    status === "completed" ||
    status === "failed" ||
    status === "cancelled" ||
    status === "interrupted"
  );
}

function readLocalAgentRuns(
  payload: unknown,
  taskId: number,
): LocalAgentRunStatusProjection[] | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const response = payload as Partial<LocalAgentRunListResponse>;
  if (response.task_id !== taskId || !Array.isArray(response.agent_runs)) {
    return null;
  }
  return response.agent_runs.filter(isLocalAgentRunStatusProjection);
}

function isLocalAgentRunStatusProjection(value: unknown): value is LocalAgentRunStatusProjection {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const run = value as Partial<LocalAgentRunStatusProjection>;
  return (
    isNonEmptyString(run.agent_run_id) &&
    isNonEmptyString(run.agent_id) &&
    isNonEmptyString(run.agent_kind) &&
    isNonEmptyString(run.agent_display_name) &&
    isLifecycleStatus(run.status) &&
    typeof run.lifecycle_version === "number" &&
    Number.isFinite(run.lifecycle_version) &&
    run.lifecycle_version > 0 &&
    typeof run.task_id === "number" &&
    Number.isFinite(run.task_id) &&
    typeof run.conversation_id === "string" &&
    typeof run.parent_turn_id === "string"
  );
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isLifecycleStatus(status: unknown): status is LocalAgentRunStatusProjection["status"] {
  return (
    status === "queued" ||
    status === "running" ||
    status === "waiting_for_approval" ||
    status === "completed" ||
    status === "failed" ||
    status === "cancelled"
  );
}
