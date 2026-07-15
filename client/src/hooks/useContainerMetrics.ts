import { useState, useEffect } from 'react';
import { metricsEventTarget } from '@/services/runtime_stream/MetricsStreamBus';
import { ContainerMetrics } from '@/types';

interface UseContainerMetricsReturn {
  metrics: ContainerMetrics | null;
  isConnected: boolean;
  error: string | null;
}

export function useContainerMetrics(taskId: string): UseContainerMetricsReturn {
  const [metrics, setMetrics] = useState<ContainerMetrics | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handleMetricsUpdate = (event: Event) => {
      const customEvent = event as CustomEvent<{ taskId: number | string; metrics: ContainerMetrics }>;
      const { taskId: eventTaskId, metrics: newMetrics } = customEvent.detail;
      
      // Only update metrics if they're for this task
      // Convert both to strings for reliable comparison
      if (String(eventTaskId) === String(taskId)) {
        setMetrics(newMetrics);
        setIsConnected(true);
        setError(null);
      }
    };

    const handleConnectionState = (event: Event) => {
      const customEvent = event as CustomEvent<{
        taskId: number | string;
        state: 'connected' | 'disconnected';
        error: string | null;
      }>;
      const detail = customEvent.detail;
      if (!detail || String(detail.taskId) !== String(taskId)) {
        return;
      }
      setIsConnected(detail.state === 'connected');
      setError(detail.error);
    };

    const handleConnectionError = (event: Event) => {
      const customEvent = event as CustomEvent<{ taskId?: number | string; error?: string | null }>;
      const detail = customEvent.detail ?? {};
      if (detail.taskId !== undefined && String(detail.taskId) !== String(taskId)) {
        return;
      }
      setIsConnected(false);
      setError(detail.error || 'Connection lost');
    };

    // Listen for metrics updates from the shared metrics websocket bus.
    metricsEventTarget.addEventListener('metrics', handleMetricsUpdate as EventListener);
    metricsEventTarget.addEventListener('connection_state', handleConnectionState as EventListener);
    metricsEventTarget.addEventListener('connection_error', handleConnectionError);

    return () => {
      metricsEventTarget.removeEventListener('metrics', handleMetricsUpdate as EventListener);
      metricsEventTarget.removeEventListener('connection_state', handleConnectionState as EventListener);
      metricsEventTarget.removeEventListener('connection_error', handleConnectionError);
    };
  }, [taskId]);

  return {
    metrics,
    isConnected,
    error,
  };
}
