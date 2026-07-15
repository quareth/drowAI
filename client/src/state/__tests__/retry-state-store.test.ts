/**
 * Tests for the task-scoped retry lifecycle store.
 *
 * The store is the single source of truth for whether a retryable failed
 * assistant turn currently has an in-flight retry; chat surfaces use it
 * to disable the retry CTA across re-renders without bloating
 * ``UnifiedAgentChat``.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  __resetRetryStateStoreForTest,
  applyRetryStateUpdate,
  clearRetryStateForTask,
  getRetryStateForTurn,
} from "@/state/retry-state-store";

afterEach(() => {
  __resetRetryStateStoreForTest();
});

describe("retry-state-store", () => {
  it("returns null for unknown task/turn pairs", () => {
    expect(getRetryStateForTurn(1, "missing")).toBeNull();
    expect(getRetryStateForTurn(null, "task-1-turn-3")).toBeNull();
    expect(getRetryStateForTurn(1, "")).toBeNull();
  });

  it("records an in-flight entry on retry POST success", () => {
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "started",
      retryAttempt: 1,
      retryMaxAttempts: 2,
    });

    const entry = getRetryStateForTurn(1, "task-1-turn-3");
    expect(entry).toMatchObject({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "started",
      inFlight: true,
      retryAttempt: 1,
      retryMaxAttempts: 2,
    });
  });

  it("treats accepted/started/retrying/waiting_for_human as in-flight", () => {
    const inFlightStates = ["accepted", "started", "retrying", "waiting_for_human"] as const;
    for (const state of inFlightStates) {
      applyRetryStateUpdate({
        taskId: 1,
        turnId: `task-1-turn-${state}`,
        workflowId: 1,
        state,
      });
      const entry = getRetryStateForTurn(1, `task-1-turn-${state}`);
      expect(entry?.inFlight, `state=${state}`).toBe(true);
    }
  });

  it("treats completed/declined/failed/cancelled as terminal (not in-flight)", () => {
    const terminalStates = ["completed", "declined", "failed", "cancelled"] as const;
    for (const state of terminalStates) {
      applyRetryStateUpdate({
        taskId: 1,
        turnId: `task-1-turn-${state}`,
        workflowId: 1,
        state,
      });
      const entry = getRetryStateForTurn(1, `task-1-turn-${state}`);
      expect(entry?.inFlight, `state=${state}`).toBe(false);
    }
  });

  it("ignores stale in-flight updates after a terminal completed for the same workflow", () => {
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "completed",
    });
    expect(getRetryStateForTurn(1, "task-1-turn-3")?.state).toBe("completed");

    // Late-arriving in-flight event for the same workflow must not
    // un-terminate the entry.
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "retrying",
    });
    expect(getRetryStateForTurn(1, "task-1-turn-3")?.state).toBe("completed");
  });

  it("ignores stale in-flight updates after a terminal cancelled for the same workflow", () => {
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "cancelled",
    });
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "retrying",
    });
    expect(getRetryStateForTurn(1, "task-1-turn-3")?.state).toBe("cancelled");
  });

  it("allows a click-driven failed → accepted optimistic transition without a new workflow_id", () => {
    // Backend has marked the previous attempt failed. The retry CTA
    // remains armed because the message is still server-marked
    // retryable; a fresh user click immediately stamps ``accepted``
    // before the mutation response carries the next workflow_id.
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "failed",
      retryAttempt: 1,
      retryMaxAttempts: 2,
    });
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: null,
      state: "accepted",
    });
    const entry = getRetryStateForTurn(1, "task-1-turn-3");
    expect(entry?.state).toBe("accepted");
    expect(entry?.inFlight).toBe(true);
  });

  it("allows a new workflow_id to replace a terminal entry on the same turn", () => {
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "failed",
      retryAttempt: 1,
      retryMaxAttempts: 2,
    });
    expect(getRetryStateForTurn(1, "task-1-turn-3")?.state).toBe("failed");

    // A second retry attempt under the budget creates a new workflow row.
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 15,
      state: "retrying",
      retryAttempt: 2,
      retryMaxAttempts: 2,
    });
    const entry = getRetryStateForTurn(1, "task-1-turn-3");
    expect(entry?.state).toBe("retrying");
    expect(entry?.workflowId).toBe(15);
    expect(entry?.inFlight).toBe(true);
  });

  it("scopes entries to (taskId, turnId) so other tasks are unaffected", () => {
    applyRetryStateUpdate({
      taskId: 1,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "retrying",
    });
    applyRetryStateUpdate({
      taskId: 2,
      turnId: "task-1-turn-3", // same turn id string, different task
      workflowId: 99,
      state: "completed",
    });

    expect(getRetryStateForTurn(1, "task-1-turn-3")?.state).toBe("retrying");
    expect(getRetryStateForTurn(2, "task-1-turn-3")?.state).toBe("completed");

    clearRetryStateForTask(1);
    expect(getRetryStateForTurn(1, "task-1-turn-3")).toBeNull();
    // Task 2 entries unaffected.
    expect(getRetryStateForTurn(2, "task-1-turn-3")?.state).toBe("completed");
  });

  it("rejects invalid inputs without writing", () => {
    applyRetryStateUpdate({
      taskId: 0,
      turnId: "task-1-turn-3",
      workflowId: 14,
      state: "retrying",
    });
    expect(getRetryStateForTurn(0, "task-1-turn-3")).toBeNull();

    applyRetryStateUpdate({
      taskId: 1,
      turnId: "",
      workflowId: 14,
      state: "retrying",
    });
    expect(getRetryStateForTurn(1, "")).toBeNull();
  });
});
