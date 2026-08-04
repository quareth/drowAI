/**
 * React hooks for reading subagent-run store state.
 *
 * Responsibilities:
 * - expose task-scoped run snapshots without mutating stream state
 * - derive conversation and parent-run views from lifecycle data
 * - compose data and presentation stores only for selected-run lookup
 */
import { useEffect, useMemo } from "react";

import { fetchLocalAgentRuns } from "../services/agent-run-replay-hydration";
import {
  getAgentRunParentGroupingKey,
  reconcileAgentRunsWithLocalStatus,
  useAgentRunStoreSnapshot,
  type AgentRunRecord,
} from "../state/agent-stream-store";
import { isAgentRunTerminalStatus } from "../contracts/agent-run";
import {
  useAgentRunPresentationSnapshot,
  type AgentRunPresentationState,
} from "../state/agent-run-presentation-store";

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

export function useSelectedAgentRun(
  taskId: number | null | undefined,
): AgentRunRecord | null {
  const snapshot = useAgentRunStoreSnapshot(taskId);
  const presentation = useAgentRunPresentationSnapshot(taskId);
  const selectedAgentRunId = presentation.selectedAgentRunId;
  if (!selectedAgentRunId) {
    return null;
  }
  return snapshot.runsById[selectedAgentRunId] ?? null;
}

export function useAgentRunPresentation(
  taskId: number | null | undefined,
): AgentRunPresentationState {
  return useAgentRunPresentationSnapshot(taskId);
}

export function useAgentRunLocalStatusHydration(
  taskId: number | null | undefined,
  isConnected: boolean,
): void {
  const snapshot = useAgentRunStoreSnapshot(taskId);
  const activeRunKey = useMemo(() => {
    if (!isValidTaskId(taskId)) {
      return "";
    }
    return snapshot.runs
      .filter(run => !isAgentRunTerminalStatus(run.status))
      .map(run => run.agentRunId)
      .sort()
      .join(",");
  }, [snapshot.runs, taskId]);

  useEffect(() => {
    if (!isValidTaskId(taskId) || !activeRunKey || !isConnected) {
      return;
    }

    const scopedTaskId = taskId;
    const controller = new AbortController();
    let cancelled = false;

    async function hydrateLocalStatus() {
      const localRuns = await fetchLocalAgentRuns(scopedTaskId, {
        signal: controller.signal,
      });
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
  }, [activeRunKey, isConnected, taskId]);
}

function isValidTaskId(taskId: number | null | undefined): taskId is number {
  return typeof taskId === "number" && Number.isFinite(taskId) && taskId > 0;
}
