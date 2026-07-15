// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useContainerMetrics } from "@/hooks/useContainerMetrics";
import { metricsEventTarget } from "@/services/runtime_stream/MetricsStreamBus";
import { metricsEventTarget as legacyMetricsEventTarget } from "@/hooks/useDockerLogs";
import type { ContainerMetrics } from "@/types";

function createMetrics(cpuPercent: number): ContainerMetrics {
  return {
    cpu_percent: cpuPercent,
    memory_usage_mb: 256,
    memory_limit_mb: 1024,
    memory_percent: 25,
    storage: {
      used_bytes: 100,
      size_root_fs: 1000,
      used_mb: 1,
      used_gb: 0.001,
    },
    network: {
      rx_bytes: 100,
      tx_bytes: 200,
    },
    timestamp: new Date().toISOString(),
  };
}

describe("useContainerMetrics", () => {
  it("updates metrics only for the matching task id", () => {
    const { result } = renderHook(() => useContainerMetrics("42"));

    act(() => {
      metricsEventTarget.dispatchEvent(
        new CustomEvent("metrics", {
          detail: { taskId: 41, metrics: createMetrics(1) },
        }),
      );
    });
    expect(result.current.metrics).toBeNull();

    act(() => {
      metricsEventTarget.dispatchEvent(
        new CustomEvent("metrics", {
          detail: { taskId: 42, metrics: createMetrics(9) },
        }),
      );
    });

    expect(result.current.metrics).toMatchObject({ cpu_percent: 9 });
    expect(result.current.isConnected).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("reflects explicit connection state updates", () => {
    const { result } = renderHook(() => useContainerMetrics("42"));

    act(() => {
      metricsEventTarget.dispatchEvent(
        new CustomEvent("connection_state", {
          detail: { taskId: 42, state: "connected", error: null },
        }),
      );
    });
    expect(result.current.isConnected).toBe(true);
    expect(result.current.error).toBeNull();

    act(() => {
      metricsEventTarget.dispatchEvent(
        new CustomEvent("connection_state", {
          detail: { taskId: 42, state: "disconnected", error: "Connection lost" },
        }),
      );
    });
    expect(result.current.isConnected).toBe(false);
    expect(result.current.error).toBe("Connection lost");
  });

  it("keeps backward-compatible metrics event target export from useDockerLogs", () => {
    expect(legacyMetricsEventTarget).toBe(metricsEventTarget);
  });
});
