/**
 * Characterizes mode orchestration's residual runtime model-switch side path.
 */
import { describe, expect, it, vi } from "vitest";

import { InteractiveModeOrchestration } from "../mode-orchestration";

describe("InteractiveModeOrchestration deployment baseline", () => {
  it("retains runtime model-switch mutation state as an SSE reconnect gate", async () => {
    const reconnect = vi.fn();
    const logger = vi.fn();
    const orchestrator = new InteractiveModeOrchestration({
      switchTaskModelMutation: { isPending: true } as any,
      sseConnection: {
        isConnected: true,
        reconnect,
        disconnect: vi.fn(),
      },
      logger,
    });

    await orchestrator.handleSSEReconnect("interactive");

    expect(reconnect).not.toHaveBeenCalled();
    expect(logger).toHaveBeenCalledWith(
      "info",
      "Delaying SSE reconnect until model switch completes",
    );
  });

  it("reconnects normally when no runtime model switch is pending", async () => {
    const reconnect = vi.fn();
    const orchestrator = new InteractiveModeOrchestration({
      switchTaskModelMutation: { isPending: false } as any,
      sseConnection: {
        isConnected: true,
        reconnect,
        disconnect: vi.fn(),
      },
    });

    await orchestrator.handleSSEReconnect("interactive");

    expect(reconnect).toHaveBeenCalledOnce();
  });
});
