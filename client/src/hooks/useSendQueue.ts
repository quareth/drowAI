import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getConversationId } from "@/utils/chatMeta";
import { useStreamingState } from "@/hooks/useStreamingState";

import type { ChatMessage } from "@/components/chat/types";


const QUEUE_DISPATCH_DELAY_MS = 500;
const STREAM_END_DEBOUNCE_MS = 700;
export interface QueueItem {
  id: string;
  content: string;
  createdAt: string;
  conversationId: string | null;
}

export interface UseSendQueueOptions {
  taskId: number | null;
  conversationId: string | null;
  messages: ChatMessage[];
  /**
   * Send function used when a message is dispatched immediately (no queue).
   * This should preserve optimistic UI behavior (e.g., useUnifiedChat.sendMessage).
   */
  sendImmediate: (content: string) => Promise<void>;
  /**
   * Send function used when dequeuing items later (auto-send). Should avoid
   * creating new optimistic entries to prevent duplicates.
   */
  sendQueued: (content: string) => Promise<void>;
  maxLength?: number; // default 4000
  onError?: (error: Error, item?: QueueItem) => void;
  maxQueueSize?: number; // soft cap, default undefined (no cap)
}

export interface SendQueueApi {
  items: QueueItem[];
  count: number;
  onUserSend: (content: string) => Promise<void>;
  remove: (id: string) => void;
  modify: (id: string, updater: ((prev: string) => string) | string) => void;
  clear: () => void;
}

// getConversationId centralized in utils/chatMeta

function getTurnId(msg: ChatMessage | undefined): string | null {
  if (!msg) return null;
  const meta = (msg.metadata ?? {}) as any;
  const id = typeof meta.id === "string" && meta.id ? meta.id : msg.id;
  return id || null;
}

function lastAgentTurnForConversation(messages: ChatMessage[], conversationId: string | null): ChatMessage | undefined {
  if (!conversationId) return undefined;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m.type !== "agent") continue;
    const cid = getConversationId(m.metadata);
    if (cid === conversationId) return m;
  }
  return undefined;
}

export function useSendQueue({
  taskId,
  conversationId,
  messages,
  sendImmediate,
  sendQueued,
  maxLength = 4000,
  onError,
  maxQueueSize,
}: UseSendQueueOptions): SendQueueApi {
  const [items, setItems] = useState<QueueItem[]>([]);

  const prevTaskRef = useRef<number | null>(null);
  const prevConvRef = useRef<string | null>(null);
  const lastTurnIdRef = useRef<string | null>(null);
  const sendingRef = useRef<boolean>(false);

  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // Track when we last sent a message to handle fast responses
  const lastSendTimeRef = useRef<number>(0);
  const isRecentSendRef = useRef<boolean>(false);

  const currentAgent = useMemo(() => lastAgentTurnForConversation(messages, conversationId), [messages, conversationId]);
  const currentTurnId = useMemo(() => getTurnId(currentAgent), [currentAgent]);

  // Track streaming state from global events
  const [, setGlobalStreamingState] = useState(false);
  const globalStreamingRef = useRef(false);
  const prevGlobalStreamingRef = useRef(false);
  const [shouldProcessQueue, setShouldProcessQueue] = useState(false);
  const streamEndTimerRef = useRef<number | null>(null);

  // Reset queue on task or conversation change
  useEffect(() => {
    const changed = prevTaskRef.current !== taskId || prevConvRef.current !== conversationId;
    if (changed) {
      setItems([]);
      lastTurnIdRef.current = null;
      sendingRef.current = false;
      isRecentSendRef.current = false;
      setGlobalStreamingState(false);
      globalStreamingRef.current = false;
      prevGlobalStreamingRef.current = false;
      setShouldProcessQueue(false);
      if (streamEndTimerRef.current) {
        clearTimeout(streamEndTimerRef.current);
      }
      streamEndTimerRef.current = null;
      prevTaskRef.current = taskId;
      prevConvRef.current = conversationId;
    }
  }, [taskId, conversationId]);

  // Listen for global streaming events
  useEffect(() => {
    // Removed console.log to prevent excessive re-renders
    
    const handleStreamingEvent = (event: CustomEvent) => {
      const detail = event.detail as { isStreaming?: boolean; taskId?: number | null } | undefined;
      if (!detail) return;
      const { isStreaming: isCurrentlyStreaming, taskId: eventTaskId } = detail;
      
      // Only update if this event is for our current task
      if (eventTaskId === taskId || eventTaskId === null) {
        // Check if we're transitioning from streaming to not streaming
        const wasStreaming = globalStreamingRef.current;
        const isNowStreaming = Boolean(isCurrentlyStreaming);
        
        // Update the previous state reference before updating current state
        prevGlobalStreamingRef.current = wasStreaming;
        globalStreamingRef.current = isNowStreaming;
        setGlobalStreamingState(isNowStreaming);

        // If we were streaming and now we're not, debounce queue processing
        if (wasStreaming && !isNowStreaming) {
          if (streamEndTimerRef.current) {
            clearTimeout(streamEndTimerRef.current);
          }
          streamEndTimerRef.current = window.setTimeout(() => {
            if (!sendingRef.current && items.length > 0) {
              setShouldProcessQueue(true);
            }
          }, STREAM_END_DEBOUNCE_MS);
        }
        
      }
    };

    window.addEventListener('llm-streaming', handleStreamingEvent as EventListener);
    
    return () => {
      // Removed console.log to prevent excessive re-renders
      window.removeEventListener('llm-streaming', handleStreamingEvent as EventListener);
    };
  }, [taskId, items.length]);

  // Preserve queue release semantics when run state updates indicate completion
  // even if no explicit llm-streaming=false event is observed.
  useEffect(() => {
    const handleRunState = (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail ?? {};
      const eventTaskId = Number((detail.taskId as number | undefined) ?? (detail.task_id as number | undefined));
      if (!Number.isFinite(eventTaskId) || eventTaskId !== taskId) {
        return;
      }
      const state = String(detail.state ?? detail.run_state ?? "").toLowerCase();
      if (!state || state === "running" || state === "waiting_for_human") {
        return;
      }
      if (!sendingRef.current && items.length > 0) {
        setShouldProcessQueue(true);
      }
    };
    window.addEventListener("task-run-state", handleRunState as EventListener);
    return () => {
      window.removeEventListener("task-run-state", handleRunState as EventListener);
    };
  }, [items.length, taskId]);

  // Single-source streaming state with fallback
  const { isStreaming } = useStreamingState({ taskId, conversationId, messages });

  const processNextItem = useCallback(() => {
    if (sendingRef.current) {
      return false;
    }

    const [nextItem] = items;
    if (!nextItem) {
      setShouldProcessQueue(false);
      return false;
    }

    sendingRef.current = true;
    setShouldProcessQueue(false);

    const run = async () => {
      try {
        await new Promise<void>((resolve) => {
          setTimeout(resolve, QUEUE_DISPATCH_DELAY_MS);
        });
        await sendQueued(nextItem.content);
        if (isMountedRef.current) {
          setItems(prev => prev.filter(item => item.id !== nextItem.id));
        }
      } catch (error) {
        console.error('[Queue] Failed to send queued message:', error);
        if (onError) onError(error instanceof Error ? error : new Error(String(error)), nextItem);
        if (isMountedRef.current) {
          setTimeout(() => {
            if (isMountedRef.current && !sendingRef.current) {
              setShouldProcessQueue(true);
            }
          }, QUEUE_DISPATCH_DELAY_MS);
        }
      } finally {
        sendingRef.current = false;
      }
    };

    void run();
    return true;
  }, [items, onError, sendQueued]);

  useEffect(() => {
    const prevTurnId = lastTurnIdRef.current;
    const prevStreaming = Boolean(prevTurnId && messages.some(m => m.type === "agent" && getTurnId(m) === prevTurnId && m.isStreaming));
    const hasQueuedItems = items.length > 0;
    const streamEnded = (prevStreaming || prevGlobalStreamingRef.current) && !isStreaming;

    if (streamEnded && hasQueuedItems && !sendingRef.current) {
      prevGlobalStreamingRef.current = false;
      if (streamEndTimerRef.current) {
        clearTimeout(streamEndTimerRef.current);
      }
      streamEndTimerRef.current = window.setTimeout(() => {
        if (!sendingRef.current && items.length > 0) {
          setShouldProcessQueue(true);
        }
      }, STREAM_END_DEBOUNCE_MS);
    }

    if (!isStreaming) {
      prevGlobalStreamingRef.current = false;
    }

    if (currentTurnId) {
      lastTurnIdRef.current = currentTurnId;
    }
  }, [currentTurnId, isStreaming, items.length, messages]);

  useEffect(() => {
    if (shouldProcessQueue && items.length > 0 && !sendingRef.current) {
      processNextItem();
    }
  }, [items.length, processNextItem, shouldProcessQueue]);

  const enqueue = useCallback((content: string) => {
    const trimmed = (content ?? "").trim();
    if (!trimmed) return;
    const safe = typeof maxLength === "number" ? trimmed.slice(0, maxLength) : trimmed;
    setItems(prev => {
      if (typeof maxQueueSize === "number" && prev.length >= maxQueueSize) {
        return prev; // soft cap; caller may toast
      }
      const item: QueueItem = {
        id: `queue-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        content: safe,
        createdAt: new Date().toISOString(),
        conversationId,
      };
      return [...prev, item];
    });
  }, [conversationId, maxLength, maxQueueSize]);

  const onUserSend = useCallback<SendQueueApi["onUserSend"]>(async (content: string) => {
    const trimmed = (content ?? "").trim();
    if (!trimmed) return;

    // Removed logging to prevent excessive re-renders
    
    // Only queue if LLM is currently streaming or a queued send is in-flight
    const shouldQueue = isStreaming || sendingRef.current || items.length > 0;
    if (shouldQueue) {
      // Removed logging to prevent excessive re-renders
      enqueue(trimmed);
      // Don't send to backend immediately when queued - wait for queue processing
      // Removed logging to prevent excessive re-renders
      return;
    }
    // Otherwise, send immediately
    // Removed logging to prevent excessive re-renders
    try {
      lastSendTimeRef.current = Date.now();
      isRecentSendRef.current = true;
      await sendImmediate(trimmed);
      // Clear the recent send flag after a delay to allow for fast response detection
      setTimeout(() => {
        isRecentSendRef.current = false;
      }, 100);
    } catch (err) {
      console.error('[Queue] Failed to send message immediately:', err);
      isRecentSendRef.current = false;
      if (onError) onError(err instanceof Error ? err : new Error(String(err)));
      throw err instanceof Error ? err : new Error(String(err));
    }
  }, [enqueue, isStreaming, onError, sendImmediate, conversationId, items.length]);

  // Clear recent send flag after streaming period
  useEffect(() => {
    const interval = setInterval(() => {
      const now = Date.now();
      if (isRecentSendRef.current && (now - lastSendTimeRef.current) > 1000) {
        isRecentSendRef.current = false;
      }
    }, 100);

    return () => clearInterval(interval);
  }, []);

  const remove = useCallback<SendQueueApi["remove"]>((id) => {
    setItems(prev => prev.filter(item => item.id !== id));
  }, []);

  const modify = useCallback<SendQueueApi["modify"]>((id, updater) => {
    setItems(prev => prev.map(item => {
      if (item.id !== id) return item;
      const nextContent = typeof updater === "function" ? (updater as (s: string) => string)(item.content) : updater;
      const trimmed = (nextContent ?? "").trim();
      const safe = typeof maxLength === "number" ? trimmed.slice(0, maxLength) : trimmed;
      return { ...item, content: safe };
    }));
  }, [maxLength]);

  const clear = useCallback(() => {
    setItems([]);
    setShouldProcessQueue(false);
  }, []);


  return {
    items,
    count: items.length,
    onUserSend,
    remove,
    modify,
    clear,
  };
}

export default useSendQueue;
