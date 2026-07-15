/**
 * Resolves persisted raw tool output text for chat tool cards.
 *
 * The hook reads raw-output provenance by task_id + tool_call_id and
 * exposes a small state machine for rendering:
 * loading | ready | not_available | error.
 */
import { useEffect, useMemo, useState } from "react";

import { apiRequest } from "@/lib/queryClient";
import type {
  ToolRawOutputBatchEntry,
  ToolRawOutputNotAvailableReason,
  ToolRawOutputBatchPayload,
  ToolRawOutputState,
  ToolRawOutputStatus,
  UseToolRawOutputOptions,
} from "@/components/chat/tool-card-terminal/toolRawOutput.types";

const settledCache = new Map<string, ToolRawOutputState>();
const inFlightCache = new Map<string, Promise<ToolRawOutputState>>();

function buildCacheKey(taskId: number | string, toolCallId: string): string {
  return `${String(taskId)}::${toolCallId}`;
}

function asResponse(value: unknown): Response {
  return value as Response;
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const payload = (await response.json().catch(() => null)) as T | null;
  if (!payload) {
    throw new Error(`Unexpected empty JSON response (${response.status})`);
  }
  return payload;
}

async function fetchRawOutputBatch(
  taskId: number | string,
  toolCallIds: string[],
): Promise<ToolRawOutputBatchPayload> {
  const response = asResponse(
    await apiRequest(
      "POST",
      `/api/artifact-provenance/tasks/${encodeURIComponent(String(taskId))}/raw-output/batch`,
      { tool_call_ids: toolCallIds },
    ),
  );
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Raw output batch lookup failed (${response.status})`);
  }
  return parseJsonResponse<ToolRawOutputBatchPayload>(response);
}

function getOrCreateLoadPromise(cacheKey: string, taskId: number | string, toolCallId: string): Promise<ToolRawOutputState> {
  const existing = inFlightCache.get(cacheKey);
  if (existing) {
    return existing;
  }
  const promise = loadToolRawOutput(taskId, toolCallId)
    .then((state) => {
      settledCache.set(cacheKey, state);
      return state;
    })
    .catch((error: unknown) => {
      const state: ToolRawOutputState = {
        status: "error",
        message: error instanceof Error ? error.message : "Failed to load tool output",
      };
      settledCache.set(cacheKey, state);
      return state;
    })
    .finally(() => {
      inFlightCache.delete(cacheKey);
    });
  inFlightCache.set(cacheKey, promise);
  return promise;
}

function normalizeBatchState(
  entry: ToolRawOutputBatchEntry | undefined,
): ToolRawOutputState | null {
  if (!entry || typeof entry !== "object") {
    return null;
  }
  if (entry.status === "ready") {
    return {
      status: "ready",
      outputText: String(entry.output_text ?? ""),
      commandArtifactId: entry.command_artifact_id ?? undefined,
      stdoutArtifactId: entry.stdout_artifact_id ?? undefined,
      stderrArtifactId: entry.stderr_artifact_id ?? undefined,
    };
  }
  if (entry.status === "not_available") {
    const reason = (entry.reason as ToolRawOutputNotAvailableReason | undefined) ?? "artifact_content_unavailable";
    return {
      status: "not_available",
      reason,
      commandArtifactId: entry.command_artifact_id ?? undefined,
      stdoutArtifactId: entry.stdout_artifact_id ?? undefined,
      stderrArtifactId: entry.stderr_artifact_id ?? undefined,
    };
  }
  if (entry.status === "error") {
    return {
      status: "error",
      message: entry.message ?? "Failed to load tool output",
    };
  }
  return null;
}

async function loadToolRawOutput(taskId: number | string, toolCallId: string): Promise<ToolRawOutputState> {
  const payload = await fetchRawOutputBatch(taskId, [toolCallId]);
  const entry = payload.results?.[toolCallId];
  const normalized = normalizeBatchState(entry);
  if (normalized) {
    return normalized;
  }
  return { status: "not_available", reason: "execution_not_found" };
}

export function primeToolRawOutputCache(taskId: number | string, toolCallId: string, state: ToolRawOutputState): void {
  const normalizedToolCallId = String(toolCallId).trim();
  if (!normalizedToolCallId) {
    return;
  }
  settledCache.set(buildCacheKey(taskId, normalizedToolCallId), state);
}

export function useToolRawOutput({ taskId, toolCallId, enabled = true }: UseToolRawOutputOptions) {
  const normalizedToolCallId = useMemo(() => {
    if (typeof toolCallId !== "string") {
      return "";
    }
    return toolCallId.trim();
  }, [toolCallId]);

  const canLoad = enabled && taskId != null && normalizedToolCallId.length > 0;
  const cacheKey = useMemo(() => {
    if (!canLoad) {
      return null;
    }
    return buildCacheKey(taskId as number | string, normalizedToolCallId);
  }, [canLoad, taskId, normalizedToolCallId]);

  const [state, setState] = useState<ToolRawOutputState>(() => {
    if (!enabled) {
      return { status: "idle" };
    }
    if (!canLoad) {
      return { status: "not_available", reason: "missing_identifiers" };
    }
    if (!cacheKey) {
      return { status: "idle" };
    }
    return settledCache.get(cacheKey) ?? { status: "loading" };
  });

  useEffect(() => {
    let cancelled = false;
    if (!enabled) {
      setState({ status: "idle" });
      return () => {
        cancelled = true;
      };
    }

    if (taskId == null || normalizedToolCallId.length === 0) {
      setState({ status: "not_available", reason: "missing_identifiers" });
      return () => {
        cancelled = true;
      };
    }

    if (!cacheKey) {
      return () => {
        cancelled = true;
      };
    }

    const cached = settledCache.get(cacheKey);
    if (cached) {
      setState(cached);
      return () => {
        cancelled = true;
      };
    }

    setState({ status: "loading" });
    void getOrCreateLoadPromise(cacheKey, taskId as number | string, normalizedToolCallId).then((nextState) => {
      if (!cancelled) {
        setState(nextState);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [cacheKey, enabled, taskId, normalizedToolCallId]);

  const status: ToolRawOutputStatus = state.status;
  return {
    state,
    status,
    isLoading: status === "loading",
    isReady: status === "ready",
    isNotAvailable: status === "not_available",
    isError: status === "error",
  };
}

export function resetToolRawOutputCacheForTests(): void {
  settledCache.clear();
  inFlightCache.clear();
}

