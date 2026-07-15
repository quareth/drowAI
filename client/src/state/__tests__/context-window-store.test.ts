/** Tests for context-window snapshot and compaction-gate store integration. */

import { afterEach, describe, expect, it } from "vitest";

import {
  applyContextCompactionLifecycleEvent,
  clearContextWindowSnapshot,
  getContextCompactionGate,
  getContextWindowSnapshot,
  releaseContextCompactionGatesForTask,
  resetContextWindowStoreForTests,
  setContextWindowSnapshot,
} from "@/state/context-window-store";

function lifecycle(state: string, sequence: number, overrides = {}) {
  return {
    task_id: 51,
    conversation_id: "conv-51",
    turn_id: "turn-51",
    epoch_id: "epoch-51",
    state,
    sequence,
    ...overrides,
  };
}

function occupancy(overrides: Record<string, unknown> = {}) {
  return {
    task_id: 51,
    conversation_id: "conv-51",
    max_tokens: 100,
    used_tokens: 80,
    remaining_tokens: 20,
    ratio: 0.8,
    ceiling_reached: false,
    recommended_next_action: "none",
    compression_candidate: false,
    turn_sequence: 1,
    revision: 1,
    snapshot_kind: "measured",
    ...overrides,
  };
}

afterEach(() => {
  resetContextWindowStoreForTests();
});

describe("context-window-store compaction integration", () => {
  it("stores lifecycle state independently from occupancy snapshots", () => {
    setContextWindowSnapshot(occupancy());
    applyContextCompactionLifecycleEvent(lifecycle("compacting", 10));

    expect(getContextCompactionGate(51, "conv-51")).toMatchObject({
      active: true,
      turnId: "turn-51",
      epochId: "epoch-51",
      lastSequence: 10,
    });

    clearContextWindowSnapshot(51, "conv-51");

    expect(getContextWindowSnapshot(51, "conv-51").taskId).toBe(0);
    expect(getContextCompactionGate(51, "conv-51")?.active).toBe(true);
  });

  it("releases only for a newer matching terminal and isolates task keys", () => {
    applyContextCompactionLifecycleEvent(lifecycle("compacting", 10));
    applyContextCompactionLifecycleEvent(
      lifecycle("completed", 11, { epoch_id: "epoch-other" }),
    );
    applyContextCompactionLifecycleEvent(
      lifecycle("completed", 11, { task_id: 52 }),
    );

    expect(getContextCompactionGate(51, "conv-51")?.active).toBe(true);
    expect(getContextCompactionGate(52, "conv-51")?.active).toBe(false);

    applyContextCompactionLifecycleEvent(lifecycle("completed", 9));
    expect(getContextCompactionGate(51, "conv-51")?.active).toBe(true);

    applyContextCompactionLifecycleEvent(lifecycle("completed", 12));
    expect(getContextCompactionGate(51, "conv-51")).toMatchObject({
      active: false,
      terminalState: "completed",
      lastSequence: 12,
    });
  });

  it("releases every active conversation gate for only the disconnected task", () => {
    applyContextCompactionLifecycleEvent(lifecycle("compacting", 10));
    applyContextCompactionLifecycleEvent(
      lifecycle("compacting", 20, {
        conversation_id: "conv-51-b",
        turn_id: "turn-51-b",
        epoch_id: "epoch-51-b",
      }),
    );
    applyContextCompactionLifecycleEvent(
      lifecycle("compacting", 30, {
        task_id: 52,
        conversation_id: "conv-52",
        turn_id: "turn-52",
        epoch_id: "epoch-52",
      }),
    );

    releaseContextCompactionGatesForTask(51);

    expect(getContextCompactionGate(51, "conv-51")).toMatchObject({
      active: false,
      lastSequence: 10,
      terminalState: null,
    });
    expect(getContextCompactionGate(51, "conv-51-b")).toMatchObject({
      active: false,
      lastSequence: 20,
      terminalState: null,
    });
    expect(getContextCompactionGate(52, "conv-52")?.active).toBe(true);

    releaseContextCompactionGatesForTask(51);
    expect(getContextCompactionGate(52, "conv-52")?.active).toBe(true);
  });

  it("keeps a measured snapshot over delayed bootstrap and equal revisions", () => {
    setContextWindowSnapshot(occupancy());
    setContextWindowSnapshot(
      occupancy({
        used_tokens: 5,
        remaining_tokens: 95,
        ratio: 0.05,
        turn_sequence: null,
        revision: -1,
        snapshot_kind: "bootstrap_estimate",
      }),
    );
    setContextWindowSnapshot(
      occupancy({
        used_tokens: 90,
        remaining_tokens: 10,
        ratio: 0.9,
      }),
    );

    expect(getContextWindowSnapshot(51, "conv-51")).toMatchObject({
      usedTokens: 80,
      revision: 1,
      snapshotKind: "measured",
    });

    setContextWindowSnapshot(
      occupancy({
        used_tokens: 92,
        remaining_tokens: 8,
        ratio: 0.92,
        turn_sequence: 2,
        revision: 2,
      }),
    );
    expect(getContextWindowSnapshot(51, "conv-51")).toMatchObject({
      usedTokens: 92,
      revision: 2,
    });
  });

  it("refreshes a changed bootstrap estimate at the sentinel revision", () => {
    setContextWindowSnapshot(
      occupancy({
        used_tokens: 20,
        remaining_tokens: 80,
        ratio: 0.2,
        turn_sequence: null,
        revision: -1,
        snapshot_kind: "bootstrap_estimate",
      }),
    );
    setContextWindowSnapshot(
      occupancy({
        used_tokens: 35,
        remaining_tokens: 65,
        ratio: 0.35,
        turn_sequence: null,
        revision: -1,
        snapshot_kind: "bootstrap_estimate",
      }),
    );

    expect(getContextWindowSnapshot(51, "conv-51")).toMatchObject({
      usedTokens: 35,
      remainingTokens: 65,
      revision: -1,
      snapshotKind: "bootstrap_estimate",
    });
  });

  it("rejects malformed newer snapshots before applying revision ordering", () => {
    setContextWindowSnapshot(occupancy());
    setContextWindowSnapshot(
      occupancy({
        used_tokens: 95,
        remaining_tokens: 20,
        ratio: 0.95,
        turn_sequence: 2,
        revision: 2,
      }),
    );

    expect(getContextWindowSnapshot(51, "conv-51")).toMatchObject({
      usedTokens: 80,
      revision: 1,
    });
  });
});
