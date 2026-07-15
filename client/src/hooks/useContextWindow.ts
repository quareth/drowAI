/**
 * Hydrates and subscribes chat-scoped context-window snapshots.
 *
 * Backend hydration and live `context-window-state` events feed the same
 * snapshot store; the backend revision decides which snapshot is current.
 */
import { useCallback, useEffect, useMemo, useState, useRef } from "react";

import { apiFetch } from "@/lib/api-config";
import { useChatSessionSnapshot } from "@/state/chat-session-store";
import {
  applyContextCompactionLifecycleEvent,
  setContextWindowSnapshot,
  useContextCompactionGate,
  useContextWindowSnapshot,
} from "@/state/context-window-store";

interface UseContextWindowOptions {
  taskId: number | null;
  conversationId?: string | null;
  enabled?: boolean;
}

interface ContextWindowEventDetail {
  taskId?: number | null;
  task_id?: number | null;
  conversationId?: string | null;
  conversation_id?: string | null;
  maxTokens?: number | null;
  max_tokens?: number | null;
  usedTokens?: number | null;
  used_tokens?: number | null;
  remainingTokens?: number | null;
  remaining_tokens?: number | null;
  ratio?: number | null;
  ceilingReached?: boolean | null;
  ceiling_reached?: boolean | null;
  recommendedNextAction?: string | null;
  recommended_next_action?: string | null;
  compressionCandidate?: boolean | null;
  compression_candidate?: boolean | null;
  turnSequence?: number | null;
  turn_sequence?: number | null;
  revision?: number | null;
  snapshotKind?: string | null;
  snapshot_kind?: string | null;
  state?: string | null;
  turnId?: string | null;
  turn_id?: string | null;
  epochId?: string | null;
  epoch_id?: string | null;
  sequence?: number | null;
  metadata?: Record<string, unknown> | null;
}

interface StreamingStateEventDetail {
  taskId?: number | null;
  task_id?: number | null;
  isStreaming?: boolean | null;
  is_streaming?: boolean | null;
}

interface ContextWindowRefreshOptions {
  force?: boolean;
}

const CONTEXT_WINDOW_REFRESH_COOLDOWN_MS = 2500;

export function useContextWindow({
  taskId,
  conversationId,
  enabled = true,
}: UseContextWindowOptions) {
  const session = useChatSessionSnapshot(taskId);
  const resolvedConversationId = useMemo(() => {
    if (typeof conversationId === "string" && conversationId.trim().length > 0) {
      return conversationId;
    }
    return session.conversationId;
  }, [conversationId, session.conversationId]);
  const snapshot = useContextWindowSnapshot(taskId, resolvedConversationId);
  const compactionGate = useContextCompactionGate(taskId, resolvedConversationId);
  const [isHydrating, setIsHydrating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const lastRefreshAtRef = useRef(0);

  const refresh = useCallback(
    (options?: ContextWindowRefreshOptions) => {
      if (!enabled || !taskId || !resolvedConversationId) {
        return;
      }
      const now = Date.now();
      const force = Boolean(options?.force);
      if (!force && now - lastRefreshAtRef.current < CONTEXT_WINDOW_REFRESH_COOLDOWN_MS) {
        return;
      }
      lastRefreshAtRef.current = now;
      setRefreshNonce((prev) => prev + 1);
    },
    [enabled, taskId, resolvedConversationId],
  );

  useEffect(() => {
    if (!enabled || !taskId || !resolvedConversationId) {
      setIsHydrating(false);
      setError(null);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const load = async () => {
      setIsHydrating(true);
      setError(null);
      try {
        const encodedConversationId = encodeURIComponent(resolvedConversationId);
        const response = await apiFetch(
          `/api/tasks/${taskId}/chat/context-window?conversation_id=${encodedConversationId}`,
          { method: "GET", signal: controller.signal },
        );
        if (!response.ok) {
          const detail = await response.text().catch(() => "");
          throw new Error(detail || `Context window fetch failed (${response.status})`);
        }
        const payload = (await response.json().catch(() => null as any)) as Record<string, unknown> | null;
        if (cancelled || !payload || typeof payload !== "object") {
          return;
        }
        setContextWindowSnapshot(payload);
      } catch (fetchError) {
        if (cancelled) return;
        if ((fetchError as Error)?.name === "AbortError") return;
        const message =
          fetchError instanceof Error ? fetchError.message : "Failed to hydrate context window";
        setError(message);
      } finally {
        if (!cancelled) {
          setIsHydrating(false);
        }
      }
    };
    void load();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [enabled, taskId, resolvedConversationId, refreshNonce]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return () => undefined;
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<ContextWindowEventDetail>).detail;
      if (!detail || typeof detail !== "object") return;
      const source =
        detail.metadata && typeof detail.metadata === "object"
          ? (detail.metadata as Record<string, unknown>)
          : (detail as Record<string, unknown>);
      applyContextCompactionLifecycleEvent(source, detail.sequence);
      setContextWindowSnapshot(source);
    };
    window.addEventListener("context-window-state", handler as EventListener);
    return () => {
      window.removeEventListener("context-window-state", handler as EventListener);
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || !enabled || !taskId) {
      return () => undefined;
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<StreamingStateEventDetail>).detail ?? {};
      const eventTaskId = Number(
        (detail.taskId as number | null | undefined) ??
        (detail.task_id as number | null | undefined),
      );
      if (!Number.isFinite(eventTaskId) || eventTaskId !== taskId) {
        return;
      }
      const isStreamingRaw = detail.isStreaming ?? detail.is_streaming;
      if (typeof isStreamingRaw === "boolean" && !isStreamingRaw) {
        refresh({ force: false });
      }
    };
    window.addEventListener("llm-streaming", handler as EventListener);
    return () => {
      window.removeEventListener("llm-streaming", handler as EventListener);
    };
  }, [enabled, refresh, taskId]);

  return {
    snapshot,
    compactionGate,
    isCompacting: Boolean(compactionGate?.active),
    isHydrating,
    error,
    taskId,
    conversationId: resolvedConversationId,
    refresh,
  };
}

export default useContextWindow;
