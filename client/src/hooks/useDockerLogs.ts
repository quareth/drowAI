/**
 * Docker log stream hook with websocket-first transport and polling fallback.
 *
 * Responsibilities:
 * - stream task runtime logs and container status
 * - fall back to tenant-aware HTTP polling when websocket is unavailable
 * - publish container metrics updates on the shared metrics event bus
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { getWebSocketUrl } from '../utils/websocket-config';
import { ContainerMetrics } from '@/types';
import {
  ChannelWebSocketTransport,
  CHANNEL_TRANSPORT_DEFAULTS,
  createChannelWebSocketTransportConfig,
} from '@/services/runtime_stream/ChannelWebSocketTransport';
import {
  emitMetricsConnectionState,
  emitMetricsUpdate,
  metricsEventTarget,
} from '@/services/runtime_stream/MetricsStreamBus';
import { apiFetch } from '@/lib/api-config';
import { onActiveTenantChanged } from '@/lib/tenant-context';

// Global event target for broadcasting metrics to other hooks. Metrics are
// delivered through the same WebSocket connection as log messages. This
// event target allows other hooks (e.g. useContainerMetrics) to subscribe
// to those updates without opening their own WebSocket.
export { metricsEventTarget };

interface LogEntry {
  timestamp: string;
  service: string;
  level: string;
  message: string;
}

interface UseDockerLogsProps {
  taskId: number | null;
  enabled?: boolean;
}

interface UseDockerLogsReturn {
  logs: LogEntry[];
  isConnected: boolean;
  connectionType: 'websocket' | 'polling' | 'disconnected';
  error: string | null;
  reconnect: () => void;
  clearLogs: () => void;
  appendLogs: (entries: LogEntry[]) => void;
  containerStatus: string | null;
  containerStatusMessage: string | null;
}

const MAX_RECONNECT_ATTEMPTS = 5;

export function useDockerLogs({ taskId, enabled = true }: UseDockerLogsProps): UseDockerLogsReturn {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [connectionType, setConnectionType] = useState<'websocket' | 'polling' | 'disconnected'>('disconnected');
  const [error, setError] = useState<string | null>(null);
  const [containerStatus, setContainerStatus] = useState<string | null>(null);
  const [containerStatusMessage, setContainerStatusMessage] = useState<string | null>(null);
  const [tenantSwitchEpoch, setTenantSwitchEpoch] = useState(0);

  const transportRef = useRef<ChannelWebSocketTransport | null>(null);
  const transportTaskIdRef = useRef<number | null>(null);
  const pollingIntervalRef = useRef<number | null>(null);
  const messageBuffer = useRef<any[]>([]);
  const batchTimeout = useRef<number>();
  const appliedTenantEpochRef = useRef(0);

  useEffect(() => {
    return onActiveTenantChanged(() => {
      setTenantSwitchEpoch((current) => current + 1);
    });
  }, []);

  const handleBatchedMessages = useCallback((messages: any[]) => {
    messages.forEach((message) => {
      if (message.type === 'log_entry') {
        setLogs((prev: LogEntry[]) => [...prev, message.data]);
      } else if (message.type === 'container_status') {
        setContainerStatus(message.status);
        setContainerStatusMessage(message.message);

        const statusLogEntry: LogEntry = {
          timestamp: message.timestamp || new Date().toISOString(),
          service: 'container-manager',
          level: message.status === 'error' ? 'error' : 'info',
          message: message.message
        };
        setLogs((prev: LogEntry[]) => [...prev, statusLogEntry]);
      } else if ((message.type === 'metrics_update' || message.type === 'metrics') && (message.metrics || message.data)) {
        const metrics = message.metrics || message.data;
        const eventTaskId = message.task_id || taskId;
        const detail = { taskId: eventTaskId, metrics: metrics as ContainerMetrics };
        emitMetricsUpdate(detail);
      } else if (message.logs && Array.isArray(message.logs)) {
        setLogs((prev: LogEntry[]) => [...prev, ...message.logs]);
      }
    });
  }, [taskId]);

  const clearLogs = useCallback(() => {
    setLogs([]);
  }, []);

  const appendLogs = useCallback((entries: LogEntry[]) => {
    if (!Array.isArray(entries) || entries.length === 0) return;
    setLogs((previous) => [...previous, ...entries]);
  }, []);

  const stopPolling = useCallback(() => {
    if (pollingIntervalRef.current) {
      window.clearInterval(pollingIntervalRef.current);
      pollingIntervalRef.current = null;
    }
  }, []);

  const resetBufferedMessages = useCallback(() => {
    messageBuffer.current = [];
    if (batchTimeout.current) {
      window.clearTimeout(batchTimeout.current);
      batchTimeout.current = undefined;
    }
  }, []);

  const disconnectTransport = useCallback((reason: string) => {
    const currentTransport = transportRef.current;
    const currentTaskId = transportTaskIdRef.current;
    const disconnectTaskId = currentTaskId ?? taskId;
    if (currentTransport) {
      setIsConnected(false);
      setConnectionType('disconnected');
      if (disconnectTaskId !== null) {
        emitMetricsConnectionState({
          taskId: disconnectTaskId,
          state: 'disconnected',
          error: null,
        });
      }
      currentTransport.disconnect(1000, reason);
    }
    transportRef.current = null;
    transportTaskIdRef.current = null;
    stopPolling();
    resetBufferedMessages();
  }, [resetBufferedMessages, stopPolling, taskId]);

  const startPolling = useCallback(async () => {
    if (!taskId || !enabled) return;

    stopPolling();
    setConnectionType('polling');

    const pollLogs = async () => {
      try {
        const token = localStorage.getItem('access_token');
        if (!token) {
          setError('Authentication required');
          setIsConnected(false);
          return;
        }

        const response = await apiFetch(`/api/docker/docker-compose/logs/${taskId}`, {
          method: 'GET',
        });

        if (response.ok) {
          const data = await response.json();
          setLogs(data.logs || []);
          setIsConnected(true);
          setError(null);
        } else if (response.status === 401) {
          setError('Session expired. Please log in again.');
          setIsConnected(false);
        } else {
          throw new Error(`HTTP ${response.status}`);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Polling failed');
        setIsConnected(false);
      }
    };

    await pollLogs();
    pollingIntervalRef.current = window.setInterval(pollLogs, 3000);
  }, [taskId, enabled, stopPolling]);

  const connectWebSocket = useCallback(() => {
    if (!taskId || !enabled) return;
    if (transportTaskIdRef.current !== null && transportTaskIdRef.current !== taskId) {
      disconnectTransport('task switched');
    }

    const existingSocket = transportRef.current?.getSocket();
    if (
      existingSocket &&
      transportTaskIdRef.current === taskId &&
      (existingSocket.readyState === WebSocket.OPEN || existingSocket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    if (transportRef.current) {
      disconnectTransport('socket reset');
    }

    const wsUrl = getWebSocketUrl('docker', taskId);
    const transport = new ChannelWebSocketTransport(createChannelWebSocketTransportConfig({
      url: wsUrl,
      runtimeDefaults: CHANNEL_TRANSPORT_DEFAULTS,
      enableReconnect: true,
      maxReconnectAttempts: MAX_RECONNECT_ATTEMPTS,
      shouldReconnect: (event) => enabled && event.code !== 1000 && event.code !== 1006,
      pingPayloadFactory: () => JSON.stringify({ type: 'ping' }),
      onMissingToken: () => {
        if (transportRef.current !== transport) return;
        setError('Authentication required for WebSocket connection');
        setIsConnected(false);
        emitMetricsConnectionState({
          taskId,
          state: 'disconnected',
          error: 'Authentication required for WebSocket connection',
        });
        startPolling();
      },
      onOpen: (socket) => {
        if (transportRef.current !== transport) return;
        setIsConnected(true);
        setConnectionType('websocket');
        setError(null);
        emitMetricsConnectionState({
          taskId,
          state: 'connected',
          error: null,
        });
        stopPolling();
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({
            type: 'init',
            taskId: taskId,
            timestamp: new Date().toISOString()
          }));
          socket.send(JSON.stringify({
            type: 'request_logs',
            taskId: taskId
          }));
        }
      },
      onMessage: (event) => {
        if (transportRef.current !== transport) return;
        try {
          const message = JSON.parse(event.data as string);
          messageBuffer.current.push(message);
          if (messageBuffer.current.length >= 10) {
            const msgs = messageBuffer.current.splice(0, messageBuffer.current.length);
            handleBatchedMessages(msgs);
          } else if (!batchTimeout.current) {
            batchTimeout.current = window.setTimeout(() => {
              const msgs = messageBuffer.current.splice(0, messageBuffer.current.length);
              handleBatchedMessages(msgs);
              batchTimeout.current = undefined;
            }, 100);
          }
        } catch (err) {
          console.error('Failed to parse WebSocket message:', err, event.data);
        }
      },
      onUnauthorized: () => {
        if (transportRef.current !== transport) return;
        setError('Session expired. Please log in again.');
        setIsConnected(false);
        setConnectionType('disconnected');
        emitMetricsConnectionState({
          taskId,
          state: 'disconnected',
          error: 'Session expired. Please log in again.',
        });
      },
      onClose: (event) => {
        if (transportRef.current !== transport) return;
        setIsConnected(false);
        emitMetricsConnectionState({
          taskId,
          state: 'disconnected',
          error: event.code === 1000 ? null : 'WebSocket closed',
        });
        if (event.code === 1008 && event.reason === 'Unauthorized') {
          return;
        }
        if (event.code === 1000) {
          setConnectionType('disconnected');
          return;
        }
        if (event.code === 1006) {
          setError('WebSocket network error, using polling fallback');
          startPolling();
        }
      },
      onError: () => {
        if (transportRef.current !== transport) return;
        setError('WebSocket connection error');
        emitMetricsConnectionState({
          taskId,
          state: 'disconnected',
          error: 'WebSocket connection error',
        });
      },
      onReconnectExhausted: () => {
        if (transportRef.current !== transport) return;
        setError('WebSocket connection failed, using polling fallback');
        emitMetricsConnectionState({
          taskId,
          state: 'disconnected',
          error: 'WebSocket connection failed, using polling fallback',
        });
        startPolling();
      },
    }));

    transportRef.current = transport;
    transportTaskIdRef.current = taskId;
    transport.connect();
  }, [disconnectTransport, enabled, handleBatchedMessages, startPolling, stopPolling, taskId]);

  const reconnect = useCallback(() => {
    setError(null);
    disconnectTransport('manual reconnect');
    connectWebSocket();
  }, [connectWebSocket, disconnectTransport]);

  useEffect(() => {
    if (!taskId || !enabled) {
      setConnectionType('disconnected');
      setIsConnected(false);
      disconnectTransport(!enabled ? 'stream disabled' : 'task unavailable');
      return;
    }

    if (appliedTenantEpochRef.current !== tenantSwitchEpoch) {
      disconnectTransport('tenant changed');
      appliedTenantEpochRef.current = tenantSwitchEpoch;
    }

    connectWebSocket();
  }, [taskId, enabled, connectWebSocket, disconnectTransport, tenantSwitchEpoch]);

  useEffect(() => {
    return () => {
      disconnectTransport('panel closed');
    };
  }, [disconnectTransport]);

  return {
    logs,
    isConnected,
    connectionType,
    error,
    reconnect,
    clearLogs,
    appendLogs,
    containerStatus,
    containerStatusMessage
  };
}
