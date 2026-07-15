/**
 * Single-authority chat bootstrap hook.
 *
 * This hook owns conversation readiness bootstrap for a task. It resolves
 * startup readiness and first transcript page via `/chat/history?initial=true`.
 * Older transcript pages are fetched only via explicit user actions.
 */
import { useEffect, useMemo, useState } from "react";

import {
  fetchInitialTranscriptPage,
  HISTORY_FETCH_TIMEOUT_MS,
  normalizeTranscriptItemsToSteps,
  seedRetryStateFromTranscriptItems,
  type ChatHistoryStartupPayload,
} from "@/hooks/chat-history-bootstrap";
import { setConversationId, useChatSessionSnapshot } from "@/state/chat-session-store";
import {
  isConversationHistoryLoading,
  markHistoryBootstrapTerminal,
  setChatReadyState,
  setHistoryLoaded,
  setHistoryLoading,
  setTranscriptPaginationState,
  setTaskHistory,
  tryStartHistoryLoading,
  useTaskStreamSnapshot,
} from "@/state/chat-stream-store";

export interface ChatBootstrapState {
  isReady: boolean;
  isPending: boolean;
  statusMessage: string | null;
  error: string | null;
}

export interface UseChatBootstrapOptions {
  taskId: number | null;
  enabled: boolean;
}

const MAX_SUBSCRIPTIONS_MESSAGE =
  "Live stream limit reached for this task. Pause or stop another active task, then try again.";

function toUserMeaningfulError(message: string | null): string | null {
  if (!message) return null;
  if (message.includes("max_subscriptions")) {
    return MAX_SUBSCRIPTIONS_MESSAGE;
  }
  return message;
}

export function useChatBootstrap({ taskId, enabled }: UseChatBootstrapOptions): ChatBootstrapState {
  const streamSnapshot = useTaskStreamSnapshot(taskId ?? null);
  const sessionSnapshot = useChatSessionSnapshot(taskId ?? null);
  const meta = (streamSnapshot.chatReadyMeta ?? {}) as Partial<ChatHistoryStartupPayload>;
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);

  const hasMeta = typeof meta === "object" && meta !== null && Object.keys(meta).length > 0;
  const serverReady = Boolean(streamSnapshot.chatReady);
  const streamConnected = streamSnapshot.isConnected;
  const streamConnecting = streamSnapshot.isConnecting;
  const connectionError = streamSnapshot.connectionError;
  const conversationId = sessionSnapshot.conversationId?.trim() || null;
  const historyLoadedForTask = Boolean(streamSnapshot.historyLoaded);
  const historyLoadedForConversation = Boolean(
    taskId && streamSnapshot.historyLoadedByConversation && streamSnapshot.historyLoadedByConversation[
      conversationId ?? "__default__"
    ],
  );
  const historyLoadingForConversation = isConversationHistoryLoading(taskId ?? null, conversationId);
  const historyLoadingForTask = Boolean(
    streamSnapshot.historyLoadingByConversation &&
    Object.keys(streamSnapshot.historyLoadingByConversation).length > 0,
  );
  const historyBootstrapTerminalForConversation = Boolean(
    taskId &&
    streamSnapshot.historyBootstrapTerminalByConversation &&
    streamSnapshot.historyBootstrapTerminalByConversation[conversationId ?? "__default__"],
  );

  useEffect(() => {
    if (!enabled || !taskId) {
      setBootstrapError(null);
      return;
    }
    if (historyLoadedForConversation || (!conversationId && historyLoadedForTask)) {
      setBootstrapError(null);
      return;
    }
    if (historyBootstrapTerminalForConversation) {
      return;
    }
    if (historyLoadingForConversation || historyLoadingForTask) {
      return;
    }

    const requestedConversationId = conversationId;
    if (!tryStartHistoryLoading(taskId, requestedConversationId)) {
      return;
    }

    const controller = new AbortController();
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, HISTORY_FETCH_TIMEOUT_MS);

    let resolvedConversationForRequest: string | null = requestedConversationId;
    setBootstrapError(null);

    const run = async () => {
      try {
        const payload = await fetchInitialTranscriptPage(taskId, {
          signal: controller.signal,
          conversationId,
        });
        if (controller.signal.aborted) return;

        const startup = payload.startup ?? null;
        if (startup) {
          const chatReadyMeta: Record<string, unknown> = { ...startup };
          setChatReadyState(taskId, Boolean(startup.chat_ready), chatReadyMeta);
        }

        const resolvedConversationId = startup?.conversation_id?.trim() || conversationId || null;
        resolvedConversationForRequest = resolvedConversationId;
        if (resolvedConversationId && resolvedConversationId !== requestedConversationId) {
          // Reserve the resolved conversation key so a same-turn rerender cannot start a second chain.
          tryStartHistoryLoading(taskId, resolvedConversationId);
        }
        if (resolvedConversationId) {
          setConversationId(taskId, resolvedConversationId);
        }

        setTranscriptPaginationState(taskId, {
          conversationId: resolvedConversationId,
          hasMoreOlder: Boolean(payload.hasMoreOlder),
          nextBeforeCursor: payload.nextBeforeTurn,
        });

        const transcriptItems = Array.isArray(payload.items) ? payload.items : [];
        // Phase 6 Task 6.3: trust the server-authoritative retry
        // projection. If any transcript item carries retry lifecycle
        // metadata, seed the retry-state store BEFORE any chat surface
        // renders the bubble so the CTA reflects the canonical state
        // (e.g. ``completed`` keeps the button disabled instead of
        // reviving the stale failed-attempt retry button).
        seedRetryStateFromTranscriptItems(taskId, transcriptItems);
        const normalizedSteps = normalizeTranscriptItemsToSteps(taskId, transcriptItems);
        if (normalizedSteps.length > 0) {
          setTaskHistory(taskId, normalizedSteps, {
            markHistoryLoaded: false,
            conversationId: resolvedConversationId,
          });
        }

        setHistoryLoaded(taskId, resolvedConversationId);
        if (!requestedConversationId && resolvedConversationId) {
          setHistoryLoaded(taskId, requestedConversationId);
        }
      } catch (error: unknown) {
        if (controller.signal.aborted && !timedOut) {
          return;
        }
        const normalized = error as Error & { status?: number };
        const match = normalized.message.match(/API Error (\d+)/);
        const statusCode = normalized.status ?? (match ? Number(match[1]) : undefined);

        let message: string;
        if (timedOut || normalized.name === "AbortError") {
          message = "Load timed out";
        } else if (statusCode === 401 || statusCode === 403) {
          message = "Authentication required to load history";
        } else if (statusCode === 404) {
          markHistoryBootstrapTerminal(taskId, requestedConversationId);
          if (resolvedConversationForRequest !== requestedConversationId) {
            markHistoryBootstrapTerminal(taskId, resolvedConversationForRequest);
          }
          message = "No history found";
        } else if (
          normalized.message.includes("timeout") ||
          normalized.message.includes("Failed to fetch") ||
          normalized.message.includes("NetworkError")
        ) {
          message = "Failed to load history";
        } else {
          message = normalized.message || "Failed to load history";
        }
        setBootstrapError(message);
      } finally {
        setHistoryLoading(taskId, false, requestedConversationId);
        if (resolvedConversationForRequest !== requestedConversationId) {
          setHistoryLoading(taskId, false, resolvedConversationForRequest);
        }
        window.clearTimeout(timeoutId);
      }
    };

    void run();
    return undefined;
  }, [
    conversationId,
    enabled,
    historyLoadedForConversation,
    historyLoadedForTask,
    historyBootstrapTerminalForConversation,
    historyLoadingForConversation,
    historyLoadingForTask,
    taskId,
  ]);

  useEffect(() => {
    if (!taskId) {
      return;
    }
    if (meta?.conversation_id) {
      setConversationId(taskId, meta.conversation_id);
      if (bootstrapError) {
        setBootstrapError(null);
      }
    }
  }, [taskId, meta?.conversation_id, bootstrapError]);

  const terminalError = historyBootstrapTerminalForConversation ? "No history found" : null;
  const error =
    toUserMeaningfulError(bootstrapError) ||
    terminalError ||
    toUserMeaningfulError(connectionError) ||
    (hasMeta && meta?.task_running === false ? "Task is not running" : null);
  const isReady = serverReady && streamConnected && !error;

  const statusMessage = useMemo(() => {
    if (!enabled || !taskId) {
      return null;
    }
    if (error) {
      return error;
    }
    if (!hasMeta) {
      return "Preparing chat...";
    }
    if (!meta?.conversation_id) {
      return "Initializing chat session...";
    }
    if (!serverReady) {
      return "Preparing chat...";
    }
    if (!streamConnected) {
      return streamConnecting ? "Connecting to live stream..." : "Reconnecting to live stream...";
    }
    return null;
  }, [
    enabled,
    taskId,
    error,
    hasMeta,
    meta?.conversation_id,
    serverReady,
    streamConnected,
    streamConnecting,
  ]);

  const isPending = enabled && taskId != null && !isReady && !error;

  return useMemo<ChatBootstrapState>(
    () => ({
      isReady,
      isPending,
      statusMessage,
      error,
    }),
    [isReady, isPending, statusMessage, error],
  );
}

export default useChatBootstrap;
