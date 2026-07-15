/**
 * Shared websocket lifecycle engine used by runtime transports.
 *
 * Responsibility:
 * - own socket connect/disconnect/reconnect flow
 * - centralize ping keepalive and optional connection-timeout handling
 * - expose close/auth/reconnect hooks without UI/state coupling
 *
 * This module is transport plumbing only and intentionally contains no
 * channel-specific protocol logic.
 */

import type { RuntimeStreamTransportCore } from "./RuntimeStreamClient";

const WS_READY_STATE_CONNECTING = 0;
const WS_READY_STATE_OPEN = 1;
const WS_CLOSE_CODE_CONNECTION_TIMEOUT = 4000;
const WS_CLOSE_REASON_CONNECTION_TIMEOUT = "Connection timeout";

export interface WebSocketLifecycleEngineConfig {
  url: string;
  transportCore: RuntimeStreamTransportCore;
  enableReconnect: boolean;
  maxReconnectAttempts?: number | null;
  shouldReconnect?: (event: CloseEvent) => boolean;
  useConnectionTimeout?: boolean;
  sendPing?: (socket: WebSocket) => void;
  onMissingToken?: () => void;
  onOpen?: (socket: WebSocket) => void;
  onMessage?: (event: MessageEvent) => void;
  onClose?: (event: CloseEvent) => void;
  onUnauthorizedClose?: (event: CloseEvent) => void;
  onError?: (event: Event) => void;
  onReconnectExhausted?: () => void;
}

export class WebSocketLifecycleEngine {
  private readonly config: WebSocketLifecycleEngineConfig;
  private socket: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopPingKeepalive: (() => void) | null = null;
  private reconnectAttempts = 0;
  private closedIntentionally = false;
  private connectionTimeout: ReturnType<typeof setTimeout> | null = null;

  public constructor(config: WebSocketLifecycleEngineConfig) {
    this.config = config;
  }

  public getSocket(): WebSocket | null {
    return this.socket;
  }

  public connect(): void {
    if (
      this.socket &&
      (
        this.socket.readyState === WS_READY_STATE_OPEN ||
        this.socket.readyState === WS_READY_STATE_CONNECTING
      )
    ) {
      return;
    }

    const token = this.config.transportCore.getToken();
    if (!token) {
      this.config.onMissingToken?.();
      return;
    }

    this.closedIntentionally = false;
    this.clearConnectionTimeout();
    this.socket = this.config.transportCore.createSocket(this.config.url, token);
    const socket = this.socket;

    if (this.config.useConnectionTimeout) {
      this.connectionTimeout = setTimeout(() => {
        if (socket.readyState === WS_READY_STATE_CONNECTING) {
          socket.close(WS_CLOSE_CODE_CONNECTION_TIMEOUT, WS_CLOSE_REASON_CONNECTION_TIMEOUT);
        }
      }, this.config.transportCore.getConnectionTimeoutMs());
    }

    socket.onopen = () => {
      this.clearConnectionTimeout();
      this.reconnectAttempts = 0;
      this.stopPingKeepalive?.();
      this.stopPingKeepalive = null;
      if (this.config.sendPing) {
        this.stopPingKeepalive = this.config.transportCore.startPingKeepalive(() => {
          const currentSocket = this.socket;
          if (!currentSocket || currentSocket.readyState !== WS_READY_STATE_OPEN) return;
          this.config.sendPing?.(currentSocket);
        });
      }
      this.config.onOpen?.(socket);
    };

    socket.onmessage = (event: MessageEvent) => {
      this.config.onMessage?.(event);
    };

    socket.onclose = (event: CloseEvent) => {
      this.clearConnectionTimeout();
      this.stopPingKeepalive?.();
      this.stopPingKeepalive = null;
      this.socket = null;
      this.config.onClose?.(event);

      if (this.closedIntentionally) {
        return;
      }

      if (this.config.transportCore.shouldTreatCloseAsUnauthorized(event)) {
        this.config.onUnauthorizedClose?.(event);
        return;
      }

      const reconnectEnabled = this.config.enableReconnect;
      const shouldReconnect = this.config.shouldReconnect
        ? this.config.shouldReconnect(event)
        : event.code !== 1000;
      if (!reconnectEnabled || !shouldReconnect) {
        return;
      }

      this.scheduleReconnect();
    };

    socket.onerror = (event: Event) => {
      this.config.onError?.(event);
    };
  }

  public disconnect(code?: number, reason?: string): void {
    this.closedIntentionally = true;
    this.clearReconnectTimer();
    this.clearConnectionTimeout();
    this.stopPingKeepalive?.();
    this.stopPingKeepalive = null;
    const socket = this.socket;
    this.socket = null;
    if (!socket) return;
    try {
      socket.close(code, reason);
    } catch {
      // no-op
    }
  }

  public sendJson(payload: unknown): void {
    const socket = this.socket;
    if (!socket || socket.readyState !== WS_READY_STATE_OPEN) return;
    socket.send(JSON.stringify(payload));
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    const maxReconnectAttempts = this.config.maxReconnectAttempts;
    if (
      maxReconnectAttempts !== undefined &&
      maxReconnectAttempts !== null &&
      this.reconnectAttempts >= maxReconnectAttempts
    ) {
      this.config.onReconnectExhausted?.();
      return;
    }

    const delay = this.config.transportCore.computeReconnectDelay(this.reconnectAttempts);
    this.reconnectAttempts += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (!this.reconnectTimer) return;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private clearConnectionTimeout(): void {
    if (!this.connectionTimeout) return;
    clearTimeout(this.connectionTimeout);
    this.connectionTimeout = null;
  }
}
