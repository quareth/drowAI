/** Tests for ordered, identity-matched context compaction gate semantics. */

import { describe, expect, it } from "vitest";

import {
  normalizeContextCompactionLifecycleEvent,
  reduceContextCompactionGate,
  type ContextCompactionGateState,
  type ContextCompactionLifecycleEvent,
  type ContextCompactionTerminalState,
} from "@/state/context-compaction-gate";

function lifecycleEvent(
  state: ContextCompactionLifecycleEvent["state"],
  sequence: number,
  overrides: Partial<ContextCompactionLifecycleEvent> = {},
): ContextCompactionLifecycleEvent {
  return {
    taskId: 41,
    conversationId: "conv-41",
    turnId: "turn-41",
    epochId: "epoch-41",
    state,
    sequence,
    ...overrides,
  };
}

describe("context-compaction-gate", () => {
  it("normalizes snake-case lifecycle identity with the stream sequence hint", () => {
    expect(
      normalizeContextCompactionLifecycleEvent(
        {
          task_id: 41,
          conversation_id: " conv-41 ",
          turn_id: " turn-41 ",
          epoch_id: " epoch-41 ",
          state: " COMPACTING ",
        },
        80,
      ),
    ).toEqual(lifecycleEvent("compacting", 80));
  });

  it("rejects packets without complete scoped identity or sequence", () => {
    expect(
      normalizeContextCompactionLifecycleEvent({
        task_id: 41,
        conversation_id: "conv-41",
        turn_id: "turn-41",
        state: "compacting",
        sequence: 80,
      }),
    ).toBeNull();
    expect(
      normalizeContextCompactionLifecycleEvent({
        task_id: 41,
        conversation_id: "conv-41",
        turn_id: "turn-41",
        epoch_id: "epoch-41",
        state: "paused",
        sequence: 80,
      }),
    ).toBeNull();
  });

  it.each<ContextCompactionTerminalState>(["completed", "failed", "cancelled"])(
    "clears the active gate for a matching %s terminal",
    (terminalState) => {
      const active = reduceContextCompactionGate(
        null,
        lifecycleEvent("compacting", 80),
      );
      const settled = reduceContextCompactionGate(
        active,
        lifecycleEvent(terminalState, 81),
      );

      expect(active.active).toBe(true);
      expect(settled).toMatchObject({
        active: false,
        turnId: "turn-41",
        epochId: "epoch-41",
        terminalState,
        lastSequence: 81,
      });
    },
  );

  it("keeps the gate active for wrong turn or epoch terminals", () => {
    const active = reduceContextCompactionGate(
      null,
      lifecycleEvent("compacting", 80),
    );
    const wrongTurn = reduceContextCompactionGate(
      active,
      lifecycleEvent("completed", 81, { turnId: "turn-other" }),
    );
    const wrongEpoch = reduceContextCompactionGate(
      wrongTurn,
      lifecycleEvent("failed", 82, { epochId: "epoch-other" }),
    );
    const matching = reduceContextCompactionGate(
      wrongEpoch,
      lifecycleEvent("cancelled", 83),
    );

    expect(wrongTurn).toBe(active);
    expect(wrongEpoch).toBe(active);
    expect(matching.active).toBe(false);
    expect(matching.terminalState).toBe("cancelled");
  });

  it("ignores stale and duplicate lifecycle packets by stream sequence", () => {
    const active = reduceContextCompactionGate(
      null,
      lifecycleEvent("compacting", 80),
    );
    const staleTerminal = reduceContextCompactionGate(
      active,
      lifecycleEvent("completed", 79),
    );
    const duplicateStart = reduceContextCompactionGate(
      staleTerminal,
      lifecycleEvent("compacting", 80),
    );

    expect(staleTerminal).toBe(active);
    expect(duplicateStart).toBe(active);
    expect(duplicateStart.active).toBe(true);
  });

  it("retains a settled sequence tombstone so stale replay cannot reactivate", () => {
    const terminalFirst = reduceContextCompactionGate(
      null,
      lifecycleEvent("completed", 90),
    );
    const staleReplay = reduceContextCompactionGate(
      terminalFirst,
      lifecycleEvent("compacting", 89),
    );
    const nextLifecycle = reduceContextCompactionGate(
      staleReplay,
      lifecycleEvent("compacting", 91, {
        turnId: "turn-42",
        epochId: "epoch-42",
      }),
    );

    expect(terminalFirst.active).toBe(false);
    expect(staleReplay).toBe(terminalFirst);
    expect(nextLifecycle).toMatchObject({
      active: true,
      turnId: "turn-42",
      epochId: "epoch-42",
      lastSequence: 91,
    });
  });

  it("does not let another task or conversation mutate an active gate", () => {
    const active: ContextCompactionGateState = reduceContextCompactionGate(
      null,
      lifecycleEvent("compacting", 80),
    );

    expect(
      reduceContextCompactionGate(
        active,
        lifecycleEvent("completed", 81, { taskId: 42 }),
      ),
    ).toBe(active);
    expect(
      reduceContextCompactionGate(
        active,
        lifecycleEvent("completed", 81, { conversationId: "conv-other" }),
      ),
    ).toBe(active);
  });
});
