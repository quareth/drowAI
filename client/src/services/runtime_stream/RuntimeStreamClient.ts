/**
 * RuntimeStreamClient manages the multiplex runtime websocket lifecycle.
 *
 * Responsibility:
 * - maintain one websocket connection for runtime stream traffic
 * - handle reconnects with exponential backoff
 * - manage per-task subscribe/unsubscribe protocol messages
 * - enforce authoritative per-task subscription state from server controls
 * - track per-task last seen sequence for replay-safe resubscribe
 *
 * This module intentionally contains no React/UI state concerns.
 */

import type {
  RuntimeAgentReasoningEnvelope,
  RuntimeStreamAuthFailureReason,
  RuntimeStreamClientMessage,
  RuntimeStreamError,
  RuntimeStreamServerMessage,
  RuntimeTaskSubscriptionErrorReason,
  RuntimeTaskSubscriptionPhase,
  RuntimeTaskSubscriptionState,
} from "./types";
import { WebSocketLifecycleEngine } from "./WebSocketLifecycleEngine";

const WS_READY_STATE_CONNECTING = 0;
const WS_READY_STATE_OPEN = 1;

export type RuntimeStreamConnectionPhase = "idle" | "connecting" | "open" | "closed";

export interface RuntimeStreamConnectionStatus {
  phase: RuntimeStreamConnectionPhase;
  error: string | null;
}

export interface RuntimeStreamClientConfig {
  url: string;
  tokenProvider: () => string | null;
  activeTenantIdProvider?: () => number | null;
  websocketFactory?: (url: string, protocols: string[]) => WebSocket;
  pingIntervalMs?: number;
  baseRetryMs?: number;
  maxRetryMs?: number;
  random?: () => number;
  onServerMessage?: (message: RuntimeStreamServerMessage) => void;
  onConnectionStatusChange?: (status: RuntimeStreamConnectionStatus) => void;
  onSubscriptionStateChange?: (taskId: number, state: RuntimeTaskSubscriptionState) => void;
  onAuthenticationFailure?: (reason: RuntimeStreamAuthFailureReason) => void;
}

export interface RuntimeStreamTransportCoreConfig {
  tokenProvider: () => string | null;
  activeTenantIdProvider?: () => number | null;
  websocketFactory: (url: string, protocols: string[]) => WebSocket;
  baseRetryMs: number;
  maxRetryMs: number;
  random: () => number;
  pingIntervalMs?: number;
  connectionTimeoutMs?: number;
}

export interface RuntimeStreamTransportCore {
  getToken: () => string | null;
  buildSubprotocols: (token: string) => string[];
  createSocket: (url: string, token: string) => WebSocket;
  computeReconnectDelay: (attempt: number) => number;
  shouldTreatCloseAsUnauthorized: (event?: CloseEvent) => boolean;
  getPingIntervalMs: () => number;
  getConnectionTimeoutMs: () => number;
  startPingKeepalive: (sendPing: () => void) => () => void;
}

const DEFAULT_PING_INTERVAL_MS = 20_000;
const DEFAULT_BASE_RETRY_MS = 1_000;
const DEFAULT_MAX_RETRY_MS = 30_000;
const SUBSCRIBE_ACK_TIMEOUT_MS = 5_000;
const UNSUBSCRIBE_ACK_TIMEOUT_MS = 5_000;
const FSM_SWEEP_INTERVAL_MS = 500;

function normalizeActiveTenantId(value: unknown): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  const parsed = typeof value === "number" ? value : Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null;
  }
  return Math.floor(parsed);
}

function defaultActiveTenantIdProvider(): number | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return normalizeActiveTenantId(window.localStorage.getItem("active_tenant_id"));
  } catch {
    return null;
  }
}

function appendTenantHintToUrl(url: string, tenantId: number | null): string {
  if (tenantId === null) {
    return url;
  }
  try {
    const parsed = new URL(url);
    if (
      parsed.searchParams.has("active_tenant_id") ||
      parsed.searchParams.has("tenant_id") ||
      parsed.searchParams.has("activeTenantId") ||
      parsed.searchParams.has("tenantId")
    ) {
      return url;
    }
    parsed.searchParams.set("active_tenant_id", String(tenantId));
    return parsed.toString();
  } catch {
    return url;
  }
}

function isPolicyAuthClose(event?: CloseEvent): boolean {
  if (!event || event.code !== 1008) {
    return false;
  }
  const normalizedReason = (event.reason ?? "").trim().toLowerCase();
  return normalizedReason === "unauthorized" || normalizedReason === "policy violation";
}

export function createRuntimeStreamTransportCore(
  config: RuntimeStreamTransportCoreConfig,
): RuntimeStreamTransportCore {
  const activeTenantIdProvider = config.activeTenantIdProvider ?? defaultActiveTenantIdProvider;
  const buildSubprotocols = (token: string) => {
    const protocols = [`Bearer.${token}`];
    const tenantId = normalizeActiveTenantId(activeTenantIdProvider());
    if (tenantId !== null) {
      protocols.push(`tenant.${tenantId}`);
    }
    return protocols;
  };
  const pingIntervalMs = config.pingIntervalMs ?? DEFAULT_PING_INTERVAL_MS;
  const connectionTimeoutMs = config.connectionTimeoutMs ?? 15_000;
  return {
    getToken: () => config.tokenProvider(),
    buildSubprotocols,
    createSocket: (url: string, token: string) => {
      const tenantId = normalizeActiveTenantId(activeTenantIdProvider());
      return config.websocketFactory(appendTenantHintToUrl(url, tenantId), buildSubprotocols(token));
    },
    computeReconnectDelay: (attempt: number) => {
      const backoff = Math.min(
        config.baseRetryMs * 2 ** Math.max(0, attempt),
        config.maxRetryMs,
      );
      const jitter = Math.floor(config.random() * 500);
      return backoff + jitter;
    },
    shouldTreatCloseAsUnauthorized: (event?: CloseEvent) => isPolicyAuthClose(event),
    getPingIntervalMs: () => pingIntervalMs,
    getConnectionTimeoutMs: () => connectionTimeoutMs,
    startPingKeepalive: (sendPing: () => void) => {
      const id = globalThis.setInterval(sendPing, pingIntervalMs);
      return () => globalThis.clearInterval(id);
    },
  };
}

export function resolveRuntimeStreamAuthFailure(
  message: RuntimeStreamServerMessage,
): RuntimeStreamAuthFailureReason | null {
  if (message.type !== "error") return null;
  const errorMessage = message as RuntimeStreamError;
  const code =
    typeof errorMessage.code === "string"
      ? errorMessage.code.trim().toLowerCase()
      : "";
  if (code === "missing_token") return "missing_token";
  if (code === "token_expired") return "token_expired";
  if (code === "invalid_token") return "invalid_token";
  if (code === "missing_exp") return "missing_exp";
  if (code === "unauthorized_identity") return "unauthorized_identity";
  if (
    code === "explicit_tenant_required" ||
    code === "tenant_membership_required" ||
    code === "inactive_tenant_membership" ||
    code === "no_active_membership" ||
    code === "invalid_tenant_hint" ||
    code === "tenant_context_missing" ||
    code === "stream_action_forbidden"
  ) {
    return "unknown_auth_error";
  }

  // Backward compatibility with older backend payloads.
  if (errorMessage.taskId !== undefined && errorMessage.taskId !== null) {
    return null;
  }
  const legacyMessage =
    typeof errorMessage.message === "string"
      ? errorMessage.message.trim()
      : "";
  if (legacyMessage === "Authentication token required") return "missing_token";
  if (legacyMessage === "Invalid authentication token") return "invalid_token";
  if (legacyMessage === "Unauthorized websocket identity") {
    return "unauthorized_identity";
  }
  return null;
}

interface TaskSubscriptionRecord {
  taskId: number;
  desired: boolean;
  phase: RuntimeTaskSubscriptionPhase;
  errorReason: RuntimeTaskSubscriptionErrorReason | null;
  updatedAt: number;
  ackDeadlineAt: number | null;
  retryAt: number | null;
  retryAttempt: number;
}

function normalizeTaskIds(taskIds: number[]): number[] {
  return Array.from(new Set(taskIds.filter((value) => Number.isFinite(value) && value > 0))).sort((a, b) => a - b);
}

function normalizeServerErrorReason(reason: unknown): RuntimeTaskSubscriptionErrorReason {
  if (typeof reason !== "string") {
    return "unknown_error";
  }
  if (reason === "forbidden_task") return "forbidden_task";
  if (reason === "max_subscriptions") return "max_subscriptions";
  if (reason === "invalid_task_id") return "invalid_task_id";
  if (reason === "subscribe_failed") return "subscribe_failed";
  return "unknown_error";
}

function isRetryableReason(reason: RuntimeTaskSubscriptionErrorReason): boolean {
  return reason === "subscribe_failed" || reason === "subscribe_timeout";
}

export class RuntimeStreamClient {
  private readonly baseRetryMs: number;
  private readonly maxRetryMs: number;
  private readonly random: () => number;
  private readonly lifecycle: WebSocketLifecycleEngine;
  private readonly onServerMessage?: (message: RuntimeStreamServerMessage) => void;
  private readonly onConnectionStatusChange?: (status: RuntimeStreamConnectionStatus) => void;
  private readonly onSubscriptionStateChange?: (taskId: number, state: RuntimeTaskSubscriptionState) => void;
  private readonly onAuthenticationFailure?: (reason: RuntimeStreamAuthFailureReason) => void;

  private socket: WebSocket | null = null;
  private shouldRun = false;
  private suppressReconnect = false;
  private authFailureReason: RuntimeStreamAuthFailureReason | null = null;
  private sweepTimer: ReturnType<typeof setInterval> | null = null;

  private desiredTaskIds = new Set<number>();
  private readonly taskSubscriptions = new Map<number, TaskSubscriptionRecord>();
  private lastSeenSequenceByTask = new Map<number, number>();
  private recoveringTaskIds = new Set<number>();
  private connectionStatus: RuntimeStreamConnectionStatus = { phase: "idle", error: null };

  public constructor(config: RuntimeStreamClientConfig) {
    const websocketFactory =
      config.websocketFactory ?? ((url: string, protocols: string[]) => new WebSocket(url, protocols));
    const pingIntervalMs = config.pingIntervalMs ?? DEFAULT_PING_INTERVAL_MS;
    this.baseRetryMs = config.baseRetryMs ?? DEFAULT_BASE_RETRY_MS;
    this.maxRetryMs = config.maxRetryMs ?? DEFAULT_MAX_RETRY_MS;
    this.random = config.random ?? Math.random;
    const transportCore = createRuntimeStreamTransportCore({
      tokenProvider: config.tokenProvider,
      activeTenantIdProvider: config.activeTenantIdProvider,
      websocketFactory,
      baseRetryMs: this.baseRetryMs,
      maxRetryMs: this.maxRetryMs,
      random: this.random,
      pingIntervalMs,
    });
    this.onServerMessage = config.onServerMessage;
    this.onConnectionStatusChange = config.onConnectionStatusChange;
    this.onSubscriptionStateChange = config.onSubscriptionStateChange;
    this.onAuthenticationFailure = config.onAuthenticationFailure;
    this.lifecycle = new WebSocketLifecycleEngine({
      url: config.url,
      transportCore,
      enableReconnect: true,
      maxReconnectAttempts: null,
      shouldReconnect: () => this.shouldRun && !this.suppressReconnect,
      useConnectionTimeout: false,
      sendPing: (socket) => {
        if (socket.readyState !== WS_READY_STATE_OPEN) return;
        socket.send(JSON.stringify({ type: "ping" }));
      },
      onMissingToken: () => {
        this.shouldRun = false;
        this.clearSweepTimer();
        this.socket = null;
        this.updateConnectionStatus({ phase: "closed", error: "Missing auth token" });
      },
      onOpen: (socket) => {
        this.socket = socket;
        this.updateConnectionStatus({ phase: "open", error: null });
        this.syncSubscriptions();
      },
      onMessage: (event) => {
        this.handleSocketMessage(event as MessageEvent<string>);
      },
      onClose: (event) => {
        this.handleSocketClose(event);
      },
      onUnauthorizedClose: () => {
        if (!this.suppressReconnect) {
          this.handleAuthenticationFailure("unknown_auth_error");
        }
      },
      onError: () => {
        // rely on close path for reconnect behavior
      },
    });
  }

  public connect(): void {
    const existingSocket = this.lifecycle.getSocket();
    if (
      existingSocket &&
      (
        existingSocket.readyState === WS_READY_STATE_OPEN ||
        existingSocket.readyState === WS_READY_STATE_CONNECTING
      )
    ) {
      return;
    }
    this.suppressReconnect = false;
    this.authFailureReason = null;
    this.shouldRun = true;
    this.startSweepTimer();
    this.updateConnectionStatus({ phase: "connecting", error: null });
    this.lifecycle.connect();
  }

  public disconnect(): void {
    this.shouldRun = false;
    this.suppressReconnect = false;
    this.authFailureReason = null;
    this.clearSweepTimer();
    this.recoveringTaskIds.clear();
    this.taskSubscriptions.clear();
    this.lifecycle.disconnect();
    this.socket = null;
    this.updateConnectionStatus({ phase: "idle", error: null });
  }

  public setDesiredTaskIds(taskIds: number[]): void {
    this.desiredTaskIds = new Set(normalizeTaskIds(taskIds));
    this.updateDesiredFlags();
    this.requeueMaxSubscriptionErrors();
    this.syncSubscriptions();
  }

  public getLastSeenSequence(taskId: number): number {
    return this.lastSeenSequenceByTask.get(taskId) ?? 0;
  }

  public getConnectionStatus(): RuntimeStreamConnectionStatus {
    return { ...this.connectionStatus };
  }

  public getTaskSubscriptionState(taskId: number): RuntimeTaskSubscriptionState {
    const normalizedTaskId = Number(taskId);
    if (!Number.isFinite(normalizedTaskId) || normalizedTaskId <= 0) {
      return {
        taskId: 0,
        desired: false,
        phase: "idle",
        errorReason: null,
        updatedAt: 0,
      };
    }
    const key = Math.floor(normalizedTaskId);
    const record = this.taskSubscriptions.get(key);
    if (!record) {
      return {
        taskId: key,
        desired: this.desiredTaskIds.has(key),
        phase: "idle",
        errorReason: null,
        updatedAt: 0,
      };
    }
    return this.toPublicSubscriptionState(record);
  }

  private handleSocketMessage(event: MessageEvent<string>): void {
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!parsed || typeof parsed !== "object") return;
    const message = parsed as RuntimeStreamServerMessage;
    const authFailure = resolveRuntimeStreamAuthFailure(message);
    if (authFailure) {
      this.handleAuthenticationFailure(authFailure);
      return;
    }
    if (!this.handleServerMessage(message)) {
      return;
    }
    this.onServerMessage?.(message);
  }

  private handleSocketClose(event: CloseEvent): void {
    this.socket = null;
    if (import.meta.env?.DEV && !(import.meta.env as any)?.VITEST) {
      // eslint-disable-next-line no-console
      console.info("[RuntimeStreamClient] socket closed", {
        code: event?.code ?? null,
        reason: event?.reason ?? null,
        wasClean: event?.wasClean ?? null,
      });
    }
    if (this.suppressReconnect) {
      this.handleSocketClosed();
      this.updateConnectionStatus({
        phase: "closed",
        error: this.authFailureReason ? this.getAuthFailureErrorMessage(this.authFailureReason) : "Authentication failed",
      });
      return;
    }
    this.handleSocketClosed();
    if (this.shouldRun) {
      this.updateConnectionStatus({ phase: "connecting", error: null });
    } else {
      this.updateConnectionStatus({ phase: "closed", error: null });
    }
  }

  private startSweepTimer(): void {
    if (this.sweepTimer) return;
    this.sweepTimer = setInterval(() => {
      this.sweepSubscriptionState();
    }, FSM_SWEEP_INTERVAL_MS);
  }

  private clearSweepTimer(): void {
    if (this.sweepTimer) {
      clearInterval(this.sweepTimer);
      this.sweepTimer = null;
    }
  }

  private handleAuthenticationFailure(reason: RuntimeStreamAuthFailureReason): void {
    this.suppressReconnect = true;
    this.authFailureReason = reason;
    this.shouldRun = false;
    this.clearSweepTimer();
    this.handleSocketClosed();
    this.lifecycle.disconnect();
    this.socket = null;

    this.updateConnectionStatus({ phase: "closed", error: this.getAuthFailureErrorMessage(reason) });
    this.onAuthenticationFailure?.(reason);
  }

  private getAuthFailureErrorMessage(reason: RuntimeStreamAuthFailureReason): string {
    if (reason === "token_expired") return "Authentication expired";
    if (reason === "missing_token") return "Authentication token required";
    if (reason === "unauthorized_identity") return "Unauthorized identity";
    if (reason === "invalid_token" || reason === "missing_exp") return "Invalid authentication token";
    return "Authentication failed";
  }

  private sweepSubscriptionState(): void {
    if (!this.shouldRun) return;
    const now = Date.now();
    const socketIsOpen = Boolean(this.socket && this.socket.readyState === WS_READY_STATE_OPEN);

    for (const taskId of Array.from(this.taskSubscriptions.keys())) {
      const record = this.ensureTaskRecord(taskId);

      if (!record.desired) {
        this.recoveringTaskIds.delete(taskId);
        if (record.phase !== "idle" || record.errorReason !== null) {
          this.transitionTask(taskId, {
            phase: "idle",
            errorReason: null,
            ackDeadlineAt: null,
            retryAt: null,
            retryAttempt: 0,
          });
        }
        this.deleteTaskRecordIfDisposable(taskId);
        continue;
      }

      if (!socketIsOpen) {
        continue;
      }

      if (record.phase === "pending_subscribe" && record.ackDeadlineAt !== null && now >= record.ackDeadlineAt) {
        this.applyTaskError(taskId, "subscribe_timeout");
        continue;
      }

      if (record.phase === "pending_unsubscribe" && record.ackDeadlineAt !== null && now >= record.ackDeadlineAt) {
        this.transitionTask(taskId, {
          phase: "idle",
          errorReason: null,
          ackDeadlineAt: null,
          retryAt: null,
          retryAttempt: 0,
        });
        if (record.desired) {
          this.sendSubscribe(taskId, this.getLastSeenSequence(taskId));
        }
        continue;
      }

      if (record.phase === "error" && record.retryAt !== null && now >= record.retryAt) {
        this.sendSubscribe(taskId, this.getLastSeenSequence(taskId));
      }
    }
  }

  private handleSocketClosed(): void {
    this.recoveringTaskIds.clear();
    for (const taskId of Array.from(this.taskSubscriptions.keys())) {
      const record = this.ensureTaskRecord(taskId);
      if (record.phase !== "idle" || record.errorReason !== null || record.ackDeadlineAt !== null || record.retryAt !== null) {
        this.transitionTask(taskId, {
          phase: "idle",
          errorReason: null,
          ackDeadlineAt: null,
          retryAt: null,
          retryAttempt: 0,
        });
      }
      this.deleteTaskRecordIfDisposable(taskId);
    }
  }

  private updateDesiredFlags(): void {
    const taskIds = new Set<number>([
      ...Array.from(this.taskSubscriptions.keys()),
      ...Array.from(this.desiredTaskIds),
    ]);
    for (const taskId of taskIds) {
      const desired = this.desiredTaskIds.has(taskId);
      const recordExists = this.taskSubscriptions.has(taskId);
      if (!recordExists && !desired) {
        continue;
      }
      this.transitionTask(taskId, { desired });
      if (!desired) {
        this.recoveringTaskIds.delete(taskId);
      }
    }
  }

  private requeueMaxSubscriptionErrors(): void {
    for (const taskId of Array.from(this.taskSubscriptions.keys())) {
      const record = this.ensureTaskRecord(taskId);
      if (!record.desired) continue;
      if (record.phase !== "error" || record.errorReason !== "max_subscriptions") continue;
      this.transitionTask(taskId, {
        phase: "idle",
        errorReason: null,
        ackDeadlineAt: null,
        retryAt: null,
        retryAttempt: 0,
      });
    }
  }

  private syncSubscriptions(): void {
    if (!this.socket || this.socket.readyState !== WS_READY_STATE_OPEN) return;

    const desiredTaskIds = normalizeTaskIds(Array.from(this.desiredTaskIds));
    for (const taskId of desiredTaskIds) {
      const record = this.ensureTaskRecord(taskId);
      if (record.phase === "idle") {
        this.sendSubscribe(taskId, this.getLastSeenSequence(taskId));
        continue;
      }
      if (record.phase === "error" && record.retryAt !== null && Date.now() >= record.retryAt) {
        this.sendSubscribe(taskId, this.getLastSeenSequence(taskId));
      }
    }

    for (const taskId of Array.from(this.taskSubscriptions.keys())) {
      if (this.desiredTaskIds.has(taskId)) continue;
      const record = this.ensureTaskRecord(taskId);
      if (record.phase === "subscribed" || record.phase === "pending_subscribe") {
        this.sendUnsubscribe(taskId);
      } else if (record.phase !== "pending_unsubscribe") {
        this.transitionTask(taskId, {
          phase: "idle",
          errorReason: null,
          ackDeadlineAt: null,
          retryAt: null,
          retryAttempt: 0,
        });
        this.deleteTaskRecordIfDisposable(taskId);
      }
    }
  }

  private sendSubscribe(taskId: number, lastSeenSequence: number): void {
    this.send({
      action: "subscribe",
      channel: "agent",
      taskId,
      last_seen_sequence: lastSeenSequence,
    });
    this.transitionTask(taskId, {
      phase: "pending_subscribe",
      errorReason: null,
      ackDeadlineAt: Date.now() + SUBSCRIBE_ACK_TIMEOUT_MS,
      retryAt: null,
    });
  }

  private sendUnsubscribe(taskId: number): void {
    this.send({
      action: "unsubscribe",
      channel: "agent",
      taskId,
    });
    this.transitionTask(taskId, {
      phase: "pending_unsubscribe",
      errorReason: null,
      ackDeadlineAt: Date.now() + UNSUBSCRIBE_ACK_TIMEOUT_MS,
      retryAt: null,
      retryAttempt: 0,
    });
  }

  private send(message: RuntimeStreamClientMessage): void {
    if (!this.socket || this.socket.readyState !== WS_READY_STATE_OPEN) return;
    this.socket.send(JSON.stringify(message));
  }

  private handleServerMessage(message: RuntimeStreamServerMessage): boolean {
    if (message.type === "agent_reasoning") {
      return this.handleAgentReasoningMessage(message);
    }

    if (message.type === "subscribed") {
      const taskId = this.toTaskId(message.taskId);
      if (taskId !== null) {
        this.transitionTask(taskId, {
          phase: "subscribed",
          errorReason: null,
          ackDeadlineAt: null,
          retryAt: null,
          retryAttempt: 0,
        });
        this.recoveringTaskIds.delete(taskId);
      }
      return true;
    }

    if (message.type === "unsubscribed") {
      const taskId = this.toTaskId(message.taskId);
      if (taskId === null) return true;
      const record = this.ensureTaskRecord(taskId);
      const previousPhase = record.phase;
      this.transitionTask(taskId, {
        phase: "idle",
        errorReason: null,
        ackDeadlineAt: null,
        retryAt: null,
        retryAttempt: 0,
      });
      this.recoveringTaskIds.delete(taskId);

      if (record.desired && (previousPhase === "pending_unsubscribe" || previousPhase === "subscribed")) {
        this.sendSubscribe(taskId, this.getLastSeenSequence(taskId));
      }

      this.requeueMaxSubscriptionErrors();
      this.syncSubscriptions();
      this.deleteTaskRecordIfDisposable(taskId);
      return true;
    }

    if (message.type === "error") {
      const taskId = this.toTaskId(message.taskId);
      if (taskId !== null) {
        this.applyTaskError(taskId, normalizeServerErrorReason(message.message));
      }
      return true;
    }

    return true;
  }

  private handleAgentReasoningMessage(message: RuntimeAgentReasoningEnvelope): boolean {
    const taskId = Number(message.taskId);
    const sequence = Number(message.sequence);
    if (!Number.isFinite(taskId) || taskId <= 0 || !Number.isFinite(sequence)) return false;
    if (!this.isTaskAcceptingPackets(taskId)) return false;

    const current = this.lastSeenSequenceByTask.get(taskId) ?? 0;
    if (!this.recoveringTaskIds.has(taskId) && current > 0 && sequence > current + 1) {
      this.triggerGapRecovery(taskId, current);
      return false;
    }

    this.trackSequence(message);
    const updated = this.lastSeenSequenceByTask.get(taskId) ?? 0;
    if (this.recoveringTaskIds.has(taskId) && updated >= current + 1) {
      this.recoveringTaskIds.delete(taskId);
    }
    return true;
  }

  private trackSequence(message: RuntimeAgentReasoningEnvelope): void {
    const taskId = Number(message.taskId);
    const sequence = Number(message.sequence);
    if (!Number.isFinite(taskId) || taskId <= 0 || !Number.isFinite(sequence)) return;
    const current = this.lastSeenSequenceByTask.get(taskId) ?? 0;
    if (sequence > current) {
      this.lastSeenSequenceByTask.set(taskId, sequence);
    }
  }

  private triggerGapRecovery(taskId: number, lastSeenSequence: number): void {
    if (this.recoveringTaskIds.has(taskId)) return;
    if (!this.socket || this.socket.readyState !== WS_READY_STATE_OPEN) return;
    if (!this.isTaskAcceptingPackets(taskId)) return;

    this.recoveringTaskIds.add(taskId);
    this.send({
      action: "unsubscribe",
      channel: "agent",
      taskId,
    });
    this.send({
      action: "subscribe",
      channel: "agent",
      taskId,
      last_seen_sequence: lastSeenSequence,
    });
    this.transitionTask(taskId, {
      phase: "pending_subscribe",
      errorReason: null,
      ackDeadlineAt: Date.now() + SUBSCRIBE_ACK_TIMEOUT_MS,
      retryAt: null,
    });
  }

  private applyTaskError(taskId: number, reason: RuntimeTaskSubscriptionErrorReason): void {
    const record = this.ensureTaskRecord(taskId);
    this.recoveringTaskIds.delete(taskId);

    if (!record.desired) {
      this.transitionTask(taskId, {
        phase: "idle",
        errorReason: null,
        ackDeadlineAt: null,
        retryAt: null,
        retryAttempt: 0,
      });
      this.deleteTaskRecordIfDisposable(taskId);
      return;
    }

    const retryable = isRetryableReason(reason);
    const nextRetryAttempt = retryable ? record.retryAttempt + 1 : 0;
    const retryAt = retryable ? Date.now() + this.computeRetryDelay(record.retryAttempt) : null;

    this.transitionTask(taskId, {
      phase: "error",
      errorReason: reason,
      ackDeadlineAt: null,
      retryAt,
      retryAttempt: nextRetryAttempt,
    });
  }

  private computeRetryDelay(attempt: number): number {
    const backoff = Math.min(this.baseRetryMs * 2 ** Math.max(0, attempt), this.maxRetryMs);
    const jitter = Math.floor(this.random() * 250);
    return backoff + jitter;
  }

  private isTaskAcceptingPackets(taskId: number): boolean {
    if (!this.desiredTaskIds.has(taskId)) {
      return false;
    }
    const phase = this.ensureTaskRecord(taskId).phase;
    return phase === "pending_subscribe" || phase === "subscribed";
  }

  private ensureTaskRecord(taskId: number): TaskSubscriptionRecord {
    const existing = this.taskSubscriptions.get(taskId);
    if (existing) return existing;

    const created: TaskSubscriptionRecord = {
      taskId,
      desired: this.desiredTaskIds.has(taskId),
      phase: "idle",
      errorReason: null,
      updatedAt: Date.now(),
      ackDeadlineAt: null,
      retryAt: null,
      retryAttempt: 0,
    };
    this.taskSubscriptions.set(taskId, created);
    return created;
  }

  private transitionTask(
    taskId: number,
    update: Partial<
      Pick<
        TaskSubscriptionRecord,
        "desired" | "phase" | "errorReason" | "ackDeadlineAt" | "retryAt" | "retryAttempt"
      >
    >,
  ): TaskSubscriptionRecord {
    const record = this.ensureTaskRecord(taskId);

    const previousDesired = record.desired;
    const previousPhase = record.phase;
    const previousErrorReason = record.errorReason;

    if ("desired" in update && update.desired !== undefined) {
      record.desired = update.desired;
    }
    if ("phase" in update && update.phase !== undefined) {
      record.phase = update.phase;
    }
    if ("errorReason" in update) {
      record.errorReason = update.errorReason ?? null;
    }
    if ("ackDeadlineAt" in update) {
      record.ackDeadlineAt = update.ackDeadlineAt ?? null;
    }
    if ("retryAt" in update) {
      record.retryAt = update.retryAt ?? null;
    }
    if ("retryAttempt" in update && update.retryAttempt !== undefined) {
      record.retryAttempt = update.retryAttempt;
    }

    const publicChanged =
      previousDesired !== record.desired ||
      previousPhase !== record.phase ||
      previousErrorReason !== record.errorReason;
    if (publicChanged) {
      record.updatedAt = Date.now();
      this.onSubscriptionStateChange?.(taskId, this.toPublicSubscriptionState(record));
    }

    return record;
  }

  private deleteTaskRecordIfDisposable(taskId: number): void {
    const record = this.taskSubscriptions.get(taskId);
    if (!record) return;
    if (record.desired) return;
    if (record.phase !== "idle") return;
    if (record.errorReason !== null) return;
    if (record.ackDeadlineAt !== null || record.retryAt !== null) return;
    this.taskSubscriptions.delete(taskId);
  }

  private toPublicSubscriptionState(record: TaskSubscriptionRecord): RuntimeTaskSubscriptionState {
    return {
      taskId: record.taskId,
      desired: record.desired,
      phase: record.phase,
      errorReason: record.errorReason,
      updatedAt: record.updatedAt,
    };
  }

  private toTaskId(value: unknown): number | null {
    const taskId = Number(value);
    if (!Number.isFinite(taskId) || taskId <= 0) return null;
    return Math.floor(taskId);
  }

  private updateConnectionStatus(next: RuntimeStreamConnectionStatus): void {
    const current = this.connectionStatus;
    if (current.phase === next.phase && current.error === next.error) {
      return;
    }
    this.connectionStatus = next;
    this.onConnectionStatusChange?.(next);
  }
}

export default RuntimeStreamClient;
