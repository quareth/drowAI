/**
 * Shared event bus for docker metrics stream fanout on the client.
 *
 * Responsibility:
 * - expose a single metrics event target independent from hook ownership
 * - publish task-scoped metrics updates from transport consumers
 * - publish task-scoped metrics connection state changes
 */

import type { ContainerMetrics } from "@/types";

export type MetricsConnectionState = "connected" | "disconnected";

export interface MetricsUpdateEventDetail {
  taskId: number | string;
  metrics: ContainerMetrics;
}

export interface MetricsConnectionEventDetail {
  taskId: number | string;
  state: MetricsConnectionState;
  error: string | null;
}

export const metricsEventTarget = new EventTarget();

export function emitMetricsUpdate(detail: MetricsUpdateEventDetail): void {
  metricsEventTarget.dispatchEvent(new CustomEvent("metrics", { detail }));
}

export function emitMetricsConnectionState(detail: MetricsConnectionEventDetail): void {
  metricsEventTarget.dispatchEvent(new CustomEvent("connection_state", { detail }));
  if (detail.error) {
    metricsEventTarget.dispatchEvent(new CustomEvent("connection_error", { detail }));
  }
}
