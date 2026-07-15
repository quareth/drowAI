import { useCallback, useEffect, useMemo, useState } from "react";

import type { ChatMessage } from "@/components/chat/types";

export interface OptimisticMessage extends ChatMessage {
  isOptimistic: true;
  status: "pending" | "confirmed" | "failed";
}

export interface OptimisticMessageOptions {
  id?: string;
  metadata?: ChatMessage["metadata"];
}

export interface UseOptimisticUpdatesOptions {
  buildMessage?: (content: string, options?: OptimisticMessageOptions) => OptimisticMessage;
  onError?: (error: Error, message: OptimisticMessage) => void;
  taskId?: number | null; // Add taskId to scope messages per task
}

export function useOptimisticUpdates({
  buildMessage,
  onError,
  taskId,
}: UseOptimisticUpdatesOptions = {}) {
  const [optimisticMessages, setOptimisticMessages] = useState<OptimisticMessage[]>([]);
  
  // Performance monitoring for message state management
  const [performanceMetrics, setPerformanceMetrics] = useState({
    messageUpdates: 0,
    stateChanges: 0,
    lastUpdateTime: 0,
  });

  // Clear optimistic messages when taskId changes
  useEffect(() => {
    setOptimisticMessages([]);
  }, [taskId]);

  // Performance monitoring disabled to prevent console noise
  // useEffect(() => {
  //   if (performanceMetrics.messageUpdates > 0) {
  //     const updateRate = performanceMetrics.messageUpdates / (performance.now() - performanceMetrics.lastUpdateTime) * 1000;
  //     console.log(`[Message State] Performance: ${performanceMetrics.messageUpdates} updates, ${performanceMetrics.stateChanges} state changes, ${updateRate.toFixed(1)} updates/s`);
  //   }
  // }, [performanceMetrics]);

  const createMessage = useMemo(
    () =>
      buildMessage ??
      ((content: string, options?: OptimisticMessageOptions): OptimisticMessage => ({
        id: options?.id ?? `optimistic-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        type: "user",
        content,
        timestamp: new Date().toISOString(),
        metadata: { status: "pending", ...(options?.metadata ?? {}) },
        isStreaming: false,
        isOptimistic: true,
        status: "pending",
      })),
    [buildMessage],
  );

  const addOptimisticMessage = useCallback(
    (content: string, options?: OptimisticMessageOptions): OptimisticMessage => {
      const optimistic = createMessage(content, options);
      const startTime = performance.now();
      
      // Performance optimization: Use functional update to avoid dependency on current state
      setOptimisticMessages((current) => {
        // Avoid unnecessary re-renders by checking if message already exists
        const exists = options?.id
          ? current.some(msg => msg.id === options.id)
          : current.some(msg => msg.content === content && msg.status === "pending");
        if (exists) return current;
        
        // Track performance metrics
        setPerformanceMetrics(prev => ({
          messageUpdates: prev.messageUpdates + 1,
          stateChanges: prev.stateChanges + 1,
          lastUpdateTime: startTime,
        }));
        
        return [...current, optimistic];
      });
      return optimistic;
    },
    [createMessage],
  );

  const confirmMessage = useCallback(
    (messageId: string, serverMessage?: ChatMessage) => {
      const startTime = performance.now();
      
      setOptimisticMessages((current) => {
        // Performance optimization: Early return if message not found
        const messageIndex = current.findIndex(msg => msg.id === messageId);
        if (messageIndex === -1) return current;
        
        // Track performance metrics
        setPerformanceMetrics(prev => ({
          messageUpdates: prev.messageUpdates + 1,
          stateChanges: prev.stateChanges + 1,
          lastUpdateTime: startTime,
        }));
        
        // Use immutable update for better performance
        const updated = [...current];
        updated[messageIndex] = {
          ...(serverMessage ?? current[messageIndex]),
          id: current[messageIndex].id,
          type: "user",
          isStreaming: false,
          isOptimistic: true,
          status: "confirmed",
          metadata: { ...(serverMessage?.metadata ?? current[messageIndex].metadata), status: "success" },
        } as OptimisticMessage;
        return updated;
      });
    },
    [],
  );

  const failMessage = useCallback(
    (messageId: string, error: string | Error) => {
      let failedMessage: OptimisticMessage | undefined;
      const startTime = performance.now();
      
      setOptimisticMessages((current) => {
        // Performance optimization: Early return if message not found
        const messageIndex = current.findIndex(msg => msg.id === messageId);
        if (messageIndex === -1) return current;
        
        // Track performance metrics
        setPerformanceMetrics(prev => ({
          messageUpdates: prev.messageUpdates + 1,
          stateChanges: prev.stateChanges + 1,
          lastUpdateTime: startTime,
        }));
        
        // Use immutable update for better performance
        const updated = [...current];
        failedMessage = {
          ...current[messageIndex],
          status: "failed",
          metadata: { ...current[messageIndex].metadata, error },
        } as OptimisticMessage;
        updated[messageIndex] = failedMessage;
        return updated;
      });

      const failure = error instanceof Error ? error : new Error(error);
      if (failedMessage) {
        onError?.(failure, failedMessage);
      }
    },
    [onError],
  );

  const clearMessage = useCallback((messageId: string) => {
    setOptimisticMessages((current) => current.filter((message) => message.id !== messageId));
  }, []);

  return {
    messages: optimisticMessages,
    addOptimisticMessage,
    confirmMessage,
    failMessage,
    clearMessage,
    performanceMetrics,
  };
}

export default useOptimisticUpdates;
