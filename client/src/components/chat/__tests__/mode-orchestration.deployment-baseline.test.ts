/**
 * Verifies mode orchestration reconnects without a task-switch side channel.
 */
import { describe, expect, it, vi } from "vitest";

import { InteractiveModeOrchestration } from "../mode-orchestration";

describe("InteractiveModeOrchestration deployment baseline", () => {
  it("reconnects without waiting for retired runtime model-switch state", async () => {
    const reconnect = vi.fn();
    const orchestrator = new InteractiveModeOrchestration({
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
