import { useCallback, useEffect, useMemo } from "react";

import type { MessageProvider } from "@/components/chat/types";
import { filterChatMessages } from "@/utils/chatFilters";
import { stepToChatMessage } from "@/utils/stepToChatMessage";
import { useOptimisticUpdates } from "@/hooks/useOptimisticUpdates";
import type { OptimisticMessage, OptimisticMessageOptions } from "@/hooks/useOptimisticUpdates";
import type { ModeOrchestrationContract } from "@/components/chat/mode-orchestration";
import { featureFlags } from "@/config/feature-flags";
import {
  fetchOlderTranscriptPage,
  HISTORY_FETCH_TIMEOUT_MS,
  normalizeTranscriptItemsToSteps,
} from "@/hooks/chat-history-bootstrap";
import { setChatState, useChatSessionSnapshot } from "@/state/chat-session-store";
import {
  setHistoryLoading,
  setTaskHistory,
  setTranscriptPaginationState,
  tryStartHistoryLoading,
  useTaskStreamSnapshot,
} from "@/state/chat-stream-store";

export interface UseUnifiedChatOptions {
  taskId: number | null;
  orchestrator: ModeOrchestrationContract;
  buildOptimisticMessage?: (content: string, options?: OptimisticMessageOptions) => OptimisticMessage;
  onSendError?: (error: Error) => void;
}

const DEFAULT_OPTIMISTIC_BUILDER = (
  content: string,
  options?: OptimisticMessageOptions,
): OptimisticMessage => ({
  id: options?.id ?? `optimistic-${Date.now()}-${Math.random().toString(36).slice(2)}`,
  type: "user",
  content,
  timestamp: new Date().toISOString(),
  metadata: { status: "pending", ...(options?.metadata ?? {}) },
  isStreaming: false,
  isOptimistic: true,
  status: "pending",
});

function createClientMessageId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function useUnifiedChat({
  taskId,
  orchestrator,
  buildOptimisticMessage = DEFAULT_OPTIMISTIC_BUILDER,
  onSendError,
}: UseUnifiedChatOptions): MessageProvider {
  const streamSnapshot = useTaskStreamSnapshot(taskId ?? null);
  const sessionSnapshot = useChatSessionSnapshot(taskId ?? null);
  const conversationId = sessionSnapshot.conversationId?.trim() || null;
  const historyConversationKey = conversationId ?? "__default__";
  const nextBeforeCursor = streamSnapshot.nextBeforeCursorByConversation[historyConversationKey] ?? null;
  const hasMoreOlder = Boolean(streamSnapshot.hasMoreOlderByConversation[historyConversationKey]);

  const {
    messages: optimisticMessages,
    addOptimisticMessage,
    failMessage,
    clearMessage,
  } = useOptimisticUpdates({
    buildMessage: buildOptimisticMessage,
    taskId, // Pass taskId to scope optimistic messages per task
    onError: (error) => {
      onSendError?.(error);
    },
  });


  const optimisticUpdatesEnabled = featureFlags.enableOptimisticUpdates;

  const normalizedMessages = useMemo(() => {
    const base = streamSnapshot.items.map(stepToChatMessage);
    return featureFlags.enableUnifiedChatFilters ? filterChatMessages(base) : base;
  }, [streamSnapshot.items]);

  // Performance optimization: Memoize message filtering to reduce re-renders
  const filteredMessages = useMemo(() => normalizedMessages, [normalizedMessages]);

  const combinedMessages = useMemo(() => {
    if (!optimisticUpdatesEnabled || !optimisticMessages.length) {
      return filteredMessages;
    }
    return [...filteredMessages, ...optimisticMessages];
  }, [filteredMessages, optimisticMessages, optimisticUpdatesEnabled]);

  useEffect(() => {
    if (!optimisticUpdatesEnabled || optimisticMessages.length === 0) {
      return;
    }
    const ackedIds = new Set<string>();
    for (const message of normalizedMessages) {
      if (message.type !== "user") continue;
      const clientId = (message.metadata as { client_message_id?: string } | undefined)?.client_message_id;
      if (clientId) {
        ackedIds.add(clientId);
      }
    }

    if (ackedIds.size === 0) {
      return;
    }

    for (const optimistic of optimisticMessages) {
      const optimisticClientId = (optimistic.metadata as { client_message_id?: string } | undefined)?.client_message_id;
      if (optimisticClientId && ackedIds.has(optimisticClientId)) {
        clearMessage(optimistic.id);
      }
    }
  }, [normalizedMessages, optimisticMessages, optimisticUpdatesEnabled, clearMessage]);

  const sendMessage = useCallback<Required<MessageProvider>["sendMessage"]>(
    async (content: string) => {
      const trimmed = content.trim();
      if (!trimmed) {
        return;
      }
      if (!taskId) {
        throw new Error("No task selected");
      }

      if (!optimisticUpdatesEnabled) {
        setChatState(taskId, "loading");
        try {
          await orchestrator.orchestrateMessageFlow(trimmed, "interactive");
        } catch (error) {
          const normalizedError = error instanceof Error ? error : new Error(String(error));
          setChatState(taskId, "input");
          onSendError?.(normalizedError);
          throw normalizedError;
        }
        return;
      }

      const clientMessageId = createClientMessageId();
      const optimistic = addOptimisticMessage(trimmed, {
        id: clientMessageId,
        metadata: { client_message_id: clientMessageId },
      });

      setChatState(taskId, "loading");
      try {
        await orchestrator.orchestrateMessageFlow(trimmed, "interactive", { clientMessageId });
      } catch (error) {
        const normalizedError = error instanceof Error ? error : new Error(String(error));
        setChatState(taskId, "input");
        failMessage(optimistic.id, normalizedError);
        throw normalizedError;
      }
    },
    [taskId, addOptimisticMessage, clearMessage, failMessage, orchestrator, optimisticUpdatesEnabled],
  );

  const loadMore = useCallback<Required<MessageProvider>["loadMore"]>(async () => {
    if (!taskId || !hasMoreOlder || typeof nextBeforeCursor !== "number" || nextBeforeCursor <= 0) {
      return;
    }
    if (!tryStartHistoryLoading(taskId, conversationId)) {
      return;
    }

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => {
      controller.abort();
    }, HISTORY_FETCH_TIMEOUT_MS);

    try {
      const payload = await fetchOlderTranscriptPage(taskId, {
        signal: controller.signal,
        conversationId,
        beforeTurn: nextBeforeCursor,
      });
      if (controller.signal.aborted) {
        return;
      }

      const transcriptItems = Array.isArray(payload.items) ? payload.items : [];
      if (transcriptItems.length > 0) {
        const normalizedSteps = normalizeTranscriptItemsToSteps(taskId, transcriptItems);
        if (normalizedSteps.length > 0) {
          setTaskHistory(taskId, normalizedSteps, {
            markHistoryLoaded: false,
            conversationId,
          });
        }
      }

      setTranscriptPaginationState(taskId, {
        conversationId,
        hasMoreOlder: Boolean(payload.hasMoreOlder),
        nextBeforeCursor: payload.nextBeforeTurn,
      });
    } finally {
      window.clearTimeout(timeoutId);
      setHistoryLoading(taskId, false, conversationId);
    }
  }, [conversationId, hasMoreOlder, nextBeforeCursor, taskId]);

  return {
    messages: combinedMessages,
    isLoading: false,
    isConnected: streamSnapshot.isConnected,
    connectionError: streamSnapshot.connectionError,
    sendMessage,
    loadMore,
    hasMore: hasMoreOlder,
  };
}

export default useUnifiedChat;
