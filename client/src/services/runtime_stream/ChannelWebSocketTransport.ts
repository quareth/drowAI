/**
 * Shared single-socket WebSocket transport for channel-style runtime streams.
 *
 * Responsibility:
 * - own one socket lifecycle (connect/disconnect/reconnect)
 * - centralize token/subprotocol handling through runtime transport core
 * - provide optional keepalive ping and reconnect exhaustion hooks
 *
 * This module is transport-only and intentionally contains no React state logic.
 */

import {
  createRuntimeStreamTransportCore,
  type RuntimeStreamTransportCore,
} from "./RuntimeStreamClient";
import { WebSocketLifecycleEngine } from "./WebSocketLifecycleEngine";

export interface ChannelWebSocketTransportConfig {
  url: string;
  tokenProvider: () => string | null;
  activeTenantIdProvider?: () => number | null;
  websocketFactory: (url: string, protocols: string[]) => WebSocket;
  baseRetryMs: number;
  maxRetryMs: number;
  random: () => number;
  pingIntervalMs?: number;
  connectionTimeoutMs?: number;
  enableReconnect?: boolean;
  maxReconnectAttempts?: number;
  shouldReconnect?: (event: CloseEvent) => boolean;
  pingPayloadFactory?: () => string;
  onOpen?: (socket: WebSocket) => void;
  onMessage?: (event: MessageEvent) => void;
  onClose?: (event: CloseEvent) => void;
  onError?: (error: Event) => void;
  onUnauthorized?: () => void;
  onMissingToken?: () => void;
  onReconnectExhausted?: () => void;
}

export interface ChannelWebSocketTransportFactoryOptions {
  url: string;
  runtimeDefaults?: {
    baseRetryMs: number;
    maxRetryMs: number;
    pingIntervalMs?: number;
    connectionTimeoutMs?: number;
  };
  tokenProvider?: () => string | null;
  activeTenantIdProvider?: () => number | null;
  websocketFactory?: (url: string, protocols: string[]) => WebSocket;
  random?: () => number;
  pingIntervalMs?: number;
  connectionTimeoutMs?: number;
  enableReconnect?: boolean;
  maxReconnectAttempts?: number;
  shouldReconnect?: (event: CloseEvent) => boolean;
  pingPayloadFactory?: () => string;
  onOpen?: (socket: WebSocket) => void;
  onMessage?: (event: MessageEvent) => void;
  onClose?: (event: CloseEvent) => void;
  onError?: (error: Event) => void;
  onUnauthorized?: () => void;
  onMissingToken?: () => void;
  onReconnectExhausted?: () => void;
}

export const CHANNEL_TRANSPORT_DEFAULTS = {
  baseRetryMs: 1_000,
  maxRetryMs: 10_000,
  pingIntervalMs: 30_000,
  connectionTimeoutMs: 15_000,
} as const;

export const TERMINAL_CHANNEL_TRANSPORT_DEFAULTS = {
  baseRetryMs: 1_000,
  maxRetryMs: 30_000,
  pingIntervalMs: 25_000,
  connectionTimeoutMs: 15_000,
} as const;

const DEFAULT_MAX_RECONNECT_ATTEMPTS = 5;

function defaultTokenProvider(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.localStorage.getItem("access_token");
  } catch {
    return null;
  }
}

function defaultWebSocketFactory(url: string, protocols: string[]): WebSocket {
  return new WebSocket(url, protocols);
}

export function createChannelWebSocketTransportConfig(
  options: ChannelWebSocketTransportFactoryOptions,
): ChannelWebSocketTransportConfig {
  const defaults = options.runtimeDefaults ?? CHANNEL_TRANSPORT_DEFAULTS;
  return {
    url: options.url,
    tokenProvider: options.tokenProvider ?? defaultTokenProvider,
    activeTenantIdProvider: options.activeTenantIdProvider,
    websocketFactory: options.websocketFactory ?? defaultWebSocketFactory,
    baseRetryMs: defaults.baseRetryMs,
    maxRetryMs: defaults.maxRetryMs,
    random: options.random ?? Math.random,
    pingIntervalMs: options.pingIntervalMs ?? defaults.pingIntervalMs,
    connectionTimeoutMs: options.connectionTimeoutMs ?? defaults.connectionTimeoutMs,
    enableReconnect: options.enableReconnect,
    maxReconnectAttempts: options.maxReconnectAttempts,
    shouldReconnect: options.shouldReconnect,
    pingPayloadFactory: options.pingPayloadFactory,
    onOpen: options.onOpen,
    onMessage: options.onMessage,
    onClose: options.onClose,
    onError: options.onError,
    onUnauthorized: options.onUnauthorized,
    onMissingToken: options.onMissingToken,
    onReconnectExhausted: options.onReconnectExhausted,
  };
}

export class ChannelWebSocketTransport {
  private readonly config: ChannelWebSocketTransportConfig;
  private readonly transportCore: RuntimeStreamTransportCore;
  private readonly lifecycle: WebSocketLifecycleEngine;

  public constructor(config: ChannelWebSocketTransportConfig) {
    this.config = config;
    this.transportCore = createRuntimeStreamTransportCore({
      tokenProvider: config.tokenProvider,
      activeTenantIdProvider: config.activeTenantIdProvider,
      websocketFactory: config.websocketFactory,
      baseRetryMs: config.baseRetryMs,
      maxRetryMs: config.maxRetryMs,
      random: config.random,
      pingIntervalMs: config.pingIntervalMs,
      connectionTimeoutMs: config.connectionTimeoutMs,
    });
    this.lifecycle = new WebSocketLifecycleEngine({
      url: config.url,
      transportCore: this.transportCore,
      enableReconnect: config.enableReconnect ?? false,
      maxReconnectAttempts: config.maxReconnectAttempts ?? DEFAULT_MAX_RECONNECT_ATTEMPTS,
      shouldReconnect: config.shouldReconnect,
      useConnectionTimeout: true,
      sendPing: (socket) => {
        const payload = this.config.pingPayloadFactory
          ? this.config.pingPayloadFactory()
          : JSON.stringify({ type: "ping" });
        socket.send(payload);
      },
      onMissingToken: () => {
        this.config.onMissingToken?.();
      },
      onOpen: (socket) => {
        this.config.onOpen?.(socket);
      },
      onMessage: (event) => {
        this.config.onMessage?.(event);
      },
      onClose: (event) => {
        this.config.onClose?.(event);
      },
      onUnauthorizedClose: () => {
        this.config.onUnauthorized?.();
      },
      onError: (event) => {
        this.config.onError?.(event);
      },
      onReconnectExhausted: () => {
        this.config.onReconnectExhausted?.();
      },
    });
  }

  public getSocket(): WebSocket | null {
    return this.lifecycle.getSocket();
  }

  public connect(): void {
    this.lifecycle.connect();
  }

  public disconnect(code?: number, reason?: string): void {
    this.lifecycle.disconnect(code, reason);
  }

  public sendJson(payload: unknown): void {
    this.lifecycle.sendJson(payload);
  }
}
