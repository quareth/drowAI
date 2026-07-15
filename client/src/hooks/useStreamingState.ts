import { useEffect, useMemo, useRef, useState } from "react";
import type { ChatMessage } from "@/components/chat/types";
import { getConversationId, isStreamingMessage } from "@/utils/chatMeta";
import { featureFlags } from "@/config/feature-flags";
import { apiFetch } from "@/lib/api-config";

interface UseStreamingStateOptions {
  taskId: number | null;
  conversationId: string | null;
  messages: ChatMessage[];
}

export function useStreamingState({ taskId, conversationId, messages }: UseStreamingStateOptions) {
  const [globalStreamingState, setGlobalStreamingState] = useState(false);
  const prevGlobalStreamingRef = useRef(false);

  useEffect(() => {
    const handler = (event: CustomEvent) => {
      const { isStreaming, taskId: eventTaskId } = (event as any).detail || {};
      if (eventTaskId === taskId || eventTaskId == null) {
        setGlobalStreamingState((prev) => {
          prevGlobalStreamingRef.current = prev;
          return Boolean(isStreaming);
        });
      }
    };
    window.addEventListener("llm-streaming", handler as EventListener);
    return () => window.removeEventListener("llm-streaming", handler as EventListener);
  }, [taskId]);

  useEffect(() => {
    prevGlobalStreamingRef.current = false;
    setGlobalStreamingState(false);
  }, [taskId]);

  useEffect(() => {
    if (taskId == null) return;
    let cancelled = false;
    const controller = new AbortController();

    const load = async () => {
      try {
        const response = await apiFetch(`/api/tasks/${taskId}/streaming-status`, {
          method: "GET",
          signal: controller.signal,
        });
        if (!response.ok) return;
        const payload = await response.json().catch(() => null as any);
        const isStreaming = Boolean(payload?.is_streaming ?? payload?.isStreaming);
        if (!cancelled) {
          setGlobalStreamingState(isStreaming);
        }
      } catch (error) {
        if ((error as Error)?.name !== "AbortError") {
          console.warn("[streaming-state] status fetch failed", taskId, error);
        }
      }
    };

    void load();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [taskId]);

  const streamingFromMessages = useMemo(() => {
    if (!conversationId) return false;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i];
      if (m.type !== "agent") continue;
      const cid = getConversationId(m.metadata);
      if (cid !== conversationId) continue;
      if (isStreamingMessage(m)) return true;
    }
    return false;
  }, [messages, conversationId]);

  // Single source of truth: trust backend/global streaming state.
  // Message-derived streaming is intentionally ignored to avoid getting stuck
  // when a client-side flag is not cleared correctly.
  const isStreaming = featureFlags.enableUnifiedStreamingState
    ? globalStreamingState
    : (globalStreamingState || streamingFromMessages);

  return { isStreaming };
}

export default useStreamingState;


