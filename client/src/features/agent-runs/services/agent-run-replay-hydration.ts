/**
 * Bounded subagent replay hydration for the process-local agent-run store.
 *
 * Responsibilities:
 * - fetch the existing task stream replay page during chat bootstrap
 * - route only subagent-attributed packets into the agent-run stream store
 * - reconcile replayed nonterminal runs with the process-local status endpoint
 */
import { apiFetch } from "@/lib/api-config";
import { advanceStreamSequence, applyStreamMessage } from "@/state/chat-stream-store";
import type { StreamEvent, StreamPacket } from "@/types/packets";

import type {
  AgentRunLifecycleStatus,
  LocalAgentRunListResponse,
  LocalAgentRunStatusProjection,
} from "../contracts/agent-run";
import {
  applyAgentRunActivityPayload,
  reconcileAgentRunsWithLocalStatus,
} from "../state/agent-stream-store";

export const AGENT_RUN_REPLAY_PAGE_LIMIT = 200;
export const AGENT_RUN_REPLAY_MAX_PAGES = 25;

export interface AgentRunReplayHydrationResult {
  replayedPackets: number;
  localStatusReconciled: boolean;
  lastSequence: number | null;
}

interface ReplayResponse {
  items?: unknown[];
  nextAfter?: unknown;
  hasMore?: unknown;
}

export async function hydrateAgentRunsFromRecentReplay(
  taskId: number,
  options?: { signal?: AbortSignal },
): Promise<AgentRunReplayHydrationResult> {
  if (!isValidTaskId(taskId)) {
    return emptyHydrationResult();
  }

  const replay = await fetchRecentTaskReplay(taskId, options);
  const replayResult = hydrateAgentRunStoreFromReplayItems(taskId, replay.items, replay.nextAfter);
  if (replayResult.replayedPackets <= 0) {
    return replayResult;
  }

  const localRuns = await fetchLocalAgentRuns(taskId, options);
  if (localRuns !== null) {
    reconcileAgentRunsWithLocalStatus(taskId, localRuns);
    return {
      ...replayResult,
      localStatusReconciled: true,
    };
  }
  return replayResult;
}

export function hydrateAgentRunStoreFromReplayItems(
  taskId: number,
  items: unknown[],
  nextAfter?: unknown,
): AgentRunReplayHydrationResult {
  if (!isValidTaskId(taskId) || !Array.isArray(items)) {
    return emptyHydrationResult();
  }

  let replayedPackets = 0;
  let lastSequence: number | null = readNonNegativeInt(nextAfter);
  for (const item of items) {
    const sequence = readReplaySequence(item);
    if (sequence !== null) {
      advanceStreamSequence(taskId, sequence);
      lastSequence = lastSequence === null ? sequence : Math.max(lastSequence, sequence);
    }
    const replayItem = item as StreamPacket | StreamEvent;
    const isAgentRunPacket = applyAgentRunActivityPayload(
      taskId,
      replayItem,
      sequence ?? undefined,
    );
    if (isAgentRunPacket) {
      replayedPackets += 1;
      applyStreamMessage(taskId, replayItem, sequence ?? undefined);
    }
  }
  if (lastSequence !== null) {
    advanceStreamSequence(taskId, lastSequence);
  }
  return {
    replayedPackets,
    localStatusReconciled: false,
    lastSequence,
  };
}

async function fetchRecentTaskReplay(
  taskId: number,
  options?: { signal?: AbortSignal },
): Promise<{ items: unknown[]; nextAfter: number | null }> {
  const items: unknown[] = [];
  let after = 0;
  let nextAfter: number | null = null;

  for (let page = 0; page < AGENT_RUN_REPLAY_MAX_PAGES; page += 1) {
    const response = await apiFetch(
      `/api/tasks/${taskId}/reasoning/replay?after=${after}&limit=${AGENT_RUN_REPLAY_PAGE_LIMIT}`,
      {
        method: "GET",
        signal: options?.signal,
      },
    );
    if (!response.ok) {
      break;
    }
    const payload = (await response.json().catch(() => null)) as ReplayResponse | null;
    const pageItems = Array.isArray(payload?.items) ? payload.items : [];
    items.push(...pageItems);
    nextAfter = readNonNegativeInt(payload?.nextAfter);
    const hasMore = payload?.hasMore === true;
    if (!hasMore || nextAfter === null || nextAfter <= after) {
      break;
    }
    after = nextAfter;
  }

  return { items, nextAfter };
}

async function fetchLocalAgentRuns(
  taskId: number,
  options?: { signal?: AbortSignal },
): Promise<LocalAgentRunStatusProjection[] | null> {
  const response = await apiFetch(`/api/tasks/${taskId}/agent-runs/local`, {
    method: "GET",
    signal: options?.signal,
  });
  if (!response.ok) {
    return null;
  }
  const payload = await response.json().catch(() => null);
  return readLocalAgentRuns(payload, taskId);
}

function readReplaySequence(value: unknown): number | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const record = value as {
    sequence?: unknown;
    obj?: { metadata?: Record<string, unknown> };
    metadata?: Record<string, unknown>;
  };
  return (
    readNonNegativeInt(record.sequence) ??
    readNonNegativeInt(record.obj?.metadata?.sequence) ??
    readNonNegativeInt(record.metadata?.sequence)
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

function isLifecycleStatus(status: unknown): status is AgentRunLifecycleStatus {
  return (
    status === "queued" ||
    status === "running" ||
    status === "waiting_for_approval" ||
    status === "completed" ||
    status === "failed" ||
    status === "cancelled"
  );
}

function readNonNegativeInt(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const normalized = Math.floor(value);
  return normalized >= 0 ? normalized : null;
}

function isValidTaskId(taskId: number): boolean {
  return Number.isFinite(taskId) && taskId > 0;
}

function emptyHydrationResult(): AgentRunReplayHydrationResult {
  return {
    replayedPackets: 0,
    localStatusReconciled: false,
    lastSequence: null,
  };
}
