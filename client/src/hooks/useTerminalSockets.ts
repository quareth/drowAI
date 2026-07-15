/**
 * Terminal websocket transport manager.
 *
 * Responsibilities:
 * - maintain one socket per terminal panel id
 * - resume/create terminal sessions across reconnects
 * - rebind active transports when tenant context changes
 */
import { useEffect, useRef } from "react";
import { wsConfig } from "@/utils/websocket-config";
import {
  ChannelWebSocketTransport,
  TERMINAL_CHANNEL_TRANSPORT_DEFAULTS,
  createChannelWebSocketTransportConfig,
} from "@/services/runtime_stream/ChannelWebSocketTransport";
import { onActiveTenantChanged } from "@/lib/tenant-context";
import {
  getTerminalSessionId,
  removeTerminalSessionId,
  setTerminalSessionId,
} from "@/lib/terminal-storage";

export interface UseTerminalSocketsOptions {
  onSessionCreated?: (
    terminalId: string,
    data: { session_id: string; session: any },
    taskId: number,
    isResume: boolean,
  ) => void;
  onBinary?: (terminalId: string, text: string, taskId: number) => void;
  onError?: (terminalId: string, message: string, taskId: number) => void;
  onClose?: (terminalId: string) => void;
}

export function useTerminalSockets(options: UseTerminalSocketsOptions = {}) {
  const transportMapRef = useRef<Map<string, ChannelWebSocketTransport>>(new Map());
  const terminalTaskMapRef = useRef<Map<string, number>>(new Map());
  const sessionMapRef = useRef<Map<string, string>>(new Map());
  const resumeModeRef = useRef<Map<string, boolean>>(new Map());
  const outputSinceConnectRef = useRef<Map<string, boolean>>(new Map());
  const nudgeTimerRef = useRef<Map<string, number>>(new Map());
  const pendingInputRef = useRef<Map<string, string[]>>(new Map());
  const inputBufferRef = useRef<Map<string, string[]>>(new Map());
  const inputFlushTimerRef = useRef<Map<string, number>>(new Map());
  const closeWaiterRef = useRef<Map<string, (closed: boolean) => void>>(new Map());
  
  const clearNudgeTimer = (terminalId: string): void => {
    const timer = nudgeTimerRef.current.get(terminalId);
    if (!timer) return;
    try {
      window.clearTimeout(timer);
    } catch {
      // no-op
    }
    nudgeTimerRef.current.delete(terminalId);
  };

  const getWebSocket = (terminalId: string): WebSocket | undefined => {
    return transportMapRef.current.get(terminalId)?.getSocket() ?? undefined;
  };

  const getSessionId = (terminalId: string): string | null => {
    return sessionMapRef.current.get(terminalId) || null;
  };

  const clearTransportState = (terminalId: string): void => {
    transportMapRef.current.delete(terminalId);
    terminalTaskMapRef.current.delete(terminalId);
    sessionMapRef.current.delete(terminalId);
    resumeModeRef.current.delete(terminalId);
    pendingInputRef.current.delete(terminalId);
    inputBufferRef.current.delete(terminalId);
    clearInputFlushTimer(terminalId);
    clearNudgeTimer(terminalId);
    outputSinceConnectRef.current.delete(terminalId);
    const waiter = closeWaiterRef.current.get(terminalId);
    if (waiter) {
      closeWaiterRef.current.delete(terminalId);
      waiter(false);
    }
  };

  const flushPendingInput = (terminalId: string, socket: WebSocket): void => {
    const queued = pendingInputRef.current.get(terminalId);
    if (!queued || queued.length === 0 || socket.readyState !== WebSocket.OPEN) return;
    pendingInputRef.current.delete(terminalId);
    for (const data of queued) {
      try {
        socket.send(JSON.stringify({ type: "input", data }));
      } catch {
        const next = pendingInputRef.current.get(terminalId) ?? [];
        next.unshift(data);
        pendingInputRef.current.set(terminalId, next);
        return;
      }
    }
  };

  const clearInputFlushTimer = (terminalId: string): void => {
    const timer = inputFlushTimerRef.current.get(terminalId);
    if (!timer) return;
    try {
      window.clearTimeout(timer);
    } catch {
      // no-op
    }
    inputFlushTimerRef.current.delete(terminalId);
  };

  const ensureConnection = (terminalId: string, taskId: number) => {
    terminalTaskMapRef.current.set(terminalId, taskId);
    const existingTransport = transportMapRef.current.get(terminalId);
    const existingSocket = existingTransport?.getSocket();
    if (existingSocket) {
      if (
        existingSocket.readyState === WebSocket.OPEN ||
        existingSocket.readyState === WebSocket.CONNECTING
      )
        return;
      existingTransport?.disconnect();
      transportMapRef.current.delete(terminalId);
    }

    const transport = new ChannelWebSocketTransport(createChannelWebSocketTransportConfig({
      url: wsConfig.getWebSocketUrl(`/ws?type=terminal&taskId=${taskId}`),
      runtimeDefaults: TERMINAL_CHANNEL_TRANSPORT_DEFAULTS,
      enableReconnect: false,
      pingPayloadFactory: () => JSON.stringify({ type: "ping" }),
      onMissingToken: () => {
        options.onError?.(terminalId, "Authentication required", taskId);
      },
      onOpen: (openedSocket) => {
        openedSocket.binaryType = "arraybuffer";
        const knownSessionId =
          sessionMapRef.current.get(terminalId) ||
          (() => {
            try {
              return getTerminalSessionId(taskId);
            } catch {
              return null;
            }
          })();
        try {
          if (knownSessionId) {
            resumeModeRef.current.set(terminalId, true);
            openedSocket.send(JSON.stringify({ type: "resume_session", session_id: knownSessionId }));
          } else {
            resumeModeRef.current.set(terminalId, false);
            openedSocket.send(JSON.stringify({ type: "create_session" }));
          }
        } catch {
          // no-op
        }
      },
      onMessage: (event) => {
        if (typeof event.data === "string") {
          try {
            const data = JSON.parse(event.data);
            if (data.type === "session_created") {
              sessionMapRef.current.set(terminalId, data.session_id);
              try {
                setTerminalSessionId(taskId, data.session_id);
              } catch {
                // no-op
              }
              const isResume = resumeModeRef.current.get(terminalId) === true;
              options.onSessionCreated?.(terminalId, data, taskId, isResume);
              const socket = transport.getSocket();
              if (socket) {
                flushPendingInput(terminalId, socket);
              }
              if (!isResume) {
                outputSinceConnectRef.current.set(terminalId, false);
                clearNudgeTimer(terminalId);
                const timer = window.setTimeout(() => {
                  const gotOutput = outputSinceConnectRef.current.get(terminalId);
                  if (!gotOutput) {
                    const socket = transport.getSocket();
                    if (socket && socket.readyState === WebSocket.OPEN) {
                      socket.send(JSON.stringify({ type: "input", data: "\n" }));
                    }
                  }
                  nudgeTimerRef.current.delete(terminalId);
                }, 500);
                nudgeTimerRef.current.set(terminalId, timer);
              }
              return;
            }
            if (data.type === "error") {
              const msg: string = String(data.message || "");
              if (resumeModeRef.current.get(terminalId) && msg.includes("Session not found")) {
                try {
                  removeTerminalSessionId(taskId);
                } catch {
                  // no-op
                }
                resumeModeRef.current.set(terminalId, false);
                const socket = transport.getSocket();
                if (socket && socket.readyState === WebSocket.OPEN) {
                  socket.send(JSON.stringify({ type: "create_session" }));
                }
                return;
              }
              options.onError?.(terminalId, msg, taskId);
              return;
            }
            if (data.type === "session_closed") {
              const closedSessionId = String(data.session_id || "");
              if (!closedSessionId || closedSessionId === sessionMapRef.current.get(terminalId)) {
                const waiter = closeWaiterRef.current.get(terminalId);
                if (waiter) {
                  closeWaiterRef.current.delete(terminalId);
                  waiter(true);
                }
              }
              return;
            }
            if (data.type === "pong") return;
          } catch {
            options.onBinary?.(terminalId, String(event.data), taskId);
          }
        } else if (event.data instanceof ArrayBuffer) {
          const text = new TextDecoder().decode(event.data);
          outputSinceConnectRef.current.set(terminalId, true);
          clearNudgeTimer(terminalId);
          options.onBinary?.(terminalId, text, taskId);
        } else if (event.data instanceof Blob) {
          const reader = new FileReader();
          reader.onload = () => {
            const text = String(reader.result || "");
            outputSinceConnectRef.current.set(terminalId, true);
            clearNudgeTimer(terminalId);
            options.onBinary?.(terminalId, text, taskId);
          };
          reader.readAsText(event.data);
        }
      },
      onClose: () => {
        const active = transportMapRef.current.get(terminalId);
        if (active === transport) {
          transportMapRef.current.delete(terminalId);
        }
        clearNudgeTimer(terminalId);
        outputSinceConnectRef.current.delete(terminalId);
        options.onClose?.(terminalId);
      },
      onError: () => {
        // no-op
      },
    }));

    transportMapRef.current.set(terminalId, transport);
    transport.connect();
  };

  const sendRawInput = (terminalId: string, data: string): boolean => {
    const socket = transportMapRef.current.get(terminalId)?.getSocket();
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({ type: "input", data }));
        return true;
      } catch {
        // Fall through to queue and reconnect below.
      }
    }

    const queued = pendingInputRef.current.get(terminalId) ?? [];
    queued.push(data);
    pendingInputRef.current.set(terminalId, queued);

    const taskId = terminalTaskMapRef.current.get(terminalId);
    if (typeof taskId === "number" && Number.isFinite(taskId) && taskId > 0) {
      ensureConnection(terminalId, taskId);
    }
    return false;
  };

  const flushBufferedInput = (terminalId: string): boolean => {
    clearInputFlushTimer(terminalId);
    const buffered = inputBufferRef.current.get(terminalId);
    if (!buffered || buffered.length === 0) return true;
    inputBufferRef.current.delete(terminalId);
    return sendRawInput(terminalId, buffered.join(""));
  };

  const sendInput = (terminalId: string, data: string): boolean => {
    const buffered = inputBufferRef.current.get(terminalId) ?? [];
    buffered.push(data);
    inputBufferRef.current.set(terminalId, buffered);
    const totalBytes = buffered.reduce((total, item) => total + item.length, 0);
    const shouldFlushNow = /[\r\n\x03\x04\x1a\x1b]/.test(data) || data.length > 1 || totalBytes >= 4096;
    if (shouldFlushNow) {
      return flushBufferedInput(terminalId);
    }
    if (!inputFlushTimerRef.current.has(terminalId)) {
      const timer = window.setTimeout(() => {
        flushBufferedInput(terminalId);
      }, 5);
      inputFlushTimerRef.current.set(terminalId, timer);
    }
    return true;
  };

  const close = (terminalId: string) => {
    const transport = transportMapRef.current.get(terminalId);
    transport?.disconnect();
    clearTransportState(terminalId);
  };

  const closeSession = async (terminalId: string, timeoutMs = 750): Promise<boolean> => {
    const transport = transportMapRef.current.get(terminalId);
    const socket = transport?.getSocket();
    const taskId = terminalTaskMapRef.current.get(terminalId);
    const sessionId =
      sessionMapRef.current.get(terminalId) ||
      (typeof taskId === "number" ? getTerminalSessionId(taskId) || undefined : undefined);
    let acknowledged = false;

    if (socket && socket.readyState === WebSocket.OPEN && sessionId) {
      acknowledged = await new Promise<boolean>((resolve) => {
        const waiter = (closed: boolean) => {
          window.clearTimeout(timer);
          resolve(closed);
        };
        const timer = window.setTimeout(() => {
          if (closeWaiterRef.current.get(terminalId) === waiter) {
            closeWaiterRef.current.delete(terminalId);
          }
          resolve(false);
        }, timeoutMs);
        closeWaiterRef.current.set(terminalId, waiter);
        try {
          socket.send(JSON.stringify({ type: "close_session", session_id: sessionId }));
        } catch {
          window.clearTimeout(timer);
          closeWaiterRef.current.delete(terminalId);
          resolve(false);
        }
      });
    }

    if (typeof taskId === "number") {
      removeTerminalSessionId(taskId);
    }
    transport?.disconnect(1000, "terminal session closed");
    clearTransportState(terminalId);
    return acknowledged;
  };

  useEffect(() => {
    return onActiveTenantChanged(() => {
      const activeTransports = Array.from(transportMapRef.current.entries());
      for (const [terminalId, transport] of activeTransports) {
        const taskId = terminalTaskMapRef.current.get(terminalId);
        transport.disconnect(1000, "tenant changed");
        transportMapRef.current.delete(terminalId);
        if (typeof taskId === "number" && Number.isFinite(taskId) && taskId > 0) {
          ensureConnection(terminalId, taskId);
        }
      }
    });
  }, []);

  // Cleanup all active transports on hook unmount.
  useEffect(() => {
    return () => {
      for (const transport of transportMapRef.current.values()) {
        transport.disconnect(1000, "terminal hook cleanup");
      }
      for (const terminalId of inputFlushTimerRef.current.keys()) {
        clearInputFlushTimer(terminalId);
      }
      transportMapRef.current.clear();
      terminalTaskMapRef.current.clear();
      sessionMapRef.current.clear();
      resumeModeRef.current.clear();
      pendingInputRef.current.clear();
      inputBufferRef.current.clear();
      closeWaiterRef.current.clear();
    };
  }, []);

  return {
    ensureConnection,
    getWebSocket,
    getSessionId,
    sendInput,
    close,
    closeSession,
  } as const;
}
