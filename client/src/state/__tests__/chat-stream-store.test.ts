import { afterEach, describe, expect, it } from "vitest";

import {
  applyStreamMessage,
  clearHistoryBootstrapTerminal,
  clearTaskState,
  getConversationHasMoreOlder,
  getConversationNextBeforeCursor,
  getTaskStreamSnapshot,
  hasConversationHistoryLoaded,
  isHistoryBootstrapTerminal,
  isConversationHistoryLoading,
  markHistoryBootstrapTerminal,
  resetTaskStreamForResync,
  setHistoryLoaded,
  setHistoryLoading,
  setTaskHistory,
  setTranscriptPaginationState,
  tryStartHistoryLoading,
} from "@/state/chat-stream-store";
import type { Step } from "@/utils/reasoning-normalizer";

const TASK_ID = 91001;

function snapshotItems(): Step[] {
  return getTaskStreamSnapshot(TASK_ID).items;
}

afterEach(() => {
  clearTaskState(TASK_ID);
});

describe("chat-stream-store persistence contracts", () => {
  it("treats assistant_final as a task-local terminal boundary", () => {
    applyStreamMessage(TASK_ID, {
      type: "reasoning_delta",
      content: "thinking",
      metadata: {
        id: "turn-terminal",
        turn_sequence: 9,
        reasoning_section_id: "turn-terminal:reasoning:0",
        step_type: "reasoning_delta",
        streaming: true,
      },
    });
    applyStreamMessage(TASK_ID, {
      type: "message_delta",
      content: "answer",
      metadata: {
        id: "turn-terminal",
        turn_sequence: 9,
        step_type: "message_delta",
        streaming: true,
      },
    });

    applyStreamMessage(TASK_ID, {
      type: "assistant_final",
      content: "answer",
      metadata: {
        id: "terminal-sentinel",
        turn_sequence: 9,
        subtype: "assistant_final",
        streaming: false,
      },
    });

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.hasStreaming).toBe(false);
    expect(snapshot.items.every((item) => item.isStreaming !== true)).toBe(true);
  });

  it("keeps terminal cleanup idempotent when assistant_final is replayed", () => {
    applyStreamMessage(TASK_ID, {
      type: "message_delta",
      content: "answer",
      metadata: { id: "turn-replay", step_type: "message_delta", streaming: true },
    });
    const terminal = {
      type: "assistant_final" as const,
      content: "answer",
      metadata: { id: "turn-replay", subtype: "assistant_final", streaming: false },
    };

    applyStreamMessage(TASK_ID, terminal);
    applyStreamMessage(TASK_ID, terminal);

    expect(getTaskStreamSnapshot(TASK_ID).hasStreaming).toBe(false);
    expect(snapshotItems().filter((item) => item.type === "assistant_final")).toHaveLength(1);
  });

  it("keeps stable order after live stream then history remount merge", () => {
    applyStreamMessage(TASK_ID, {
      type: "reasoning_delta",
      content: "thinking",
      metadata: {
        id: "turn-1",
        ind: 0,
        turn_sequence: 1,
        sequence: 10,
        step_type: "reasoning_delta",
        reasoning_section_id: "turn-1:reasoning:0",
        phase_sequence: 0,
      },
    });
    applyStreamMessage(TASK_ID, {
      type: "tool_start",
      content: "start",
      metadata: {
        id: "turn-1",
        ind: 1,
        turn_sequence: 1,
        sequence: 11,
        step_type: "tool_start",
        tool_call_id: "call-1",
      },
    });
    applyStreamMessage(TASK_ID, {
      type: "tool_end",
      content: "done",
      metadata: {
        id: "turn-1",
        ind: 1,
        turn_sequence: 1,
        sequence: 13,
        step_type: "tool_end",
        tool_call_id: "call-1",
      },
    });

    // Simulate route remount: history arrives again, including overlap.
    setTaskHistory(TASK_ID, [
      {
        type: "tool_delta",
        content: "delta",
        metadata: {
          id: "turn-1",
          ind: 1,
          turn_sequence: 1,
          sequence: 12,
          step_type: "tool_delta",
          tool_call_id: "call-1",
        },
      },
      {
        type: "tool_end",
        content: "done",
        metadata: {
          id: "turn-1",
          ind: 1,
          turn_sequence: 1,
          sequence: 13,
          step_type: "tool_end",
          tool_call_id: "call-1",
        },
      },
      {
        type: "message_delta",
        content: "answer",
        metadata: { id: "turn-1", ind: 2, turn_sequence: 1, sequence: 14, step_type: "message_delta" },
      },
    ]);

    const items = snapshotItems();
    expect(items.map((item) => item.type)).toEqual([
      "reasoning_delta",
      "tool_start",
      "tool_delta",
      "tool_end",
      "message_delta",
    ]);
  });

  it("refresh-like history replay does not duplicate identical logical events", () => {
    const replaySteps: Step[] = [
      {
        type: "observation_delta",
        content: "obs one",
        metadata: {
          id: "turn-2",
          ind: 3,
          turn_sequence: 2,
          sequence: 20,
          step_type: "observation_delta",
          sub_turn_index: 0,
        },
      },
      {
        type: "observation_delta",
        content: "obs two",
        metadata: {
          id: "turn-2",
          ind: 3,
          turn_sequence: 2,
          sequence: 21,
          step_type: "observation_delta",
          sub_turn_index: 1,
        },
      },
    ];

    setTaskHistory(TASK_ID, replaySteps);
    setTaskHistory(TASK_ID, replaySteps);

    const items = snapshotItems().filter((item) => item.type === "observation_delta");
    expect(items).toHaveLength(2);
    expect(items.map((item) => item.content)).toEqual(["obs one", "obs two"]);
  });

  it("dedupes first observation when live omits sub_turn_index and replay backfills 0", () => {
    applyStreamMessage(TASK_ID, {
      type: "observation_delta",
      content: "obs one",
      metadata: {
        id: "turn-3",
        ind: 3,
        turn_sequence: 3,
        sequence: 30,
        step_type: "observation_delta",
      },
    });

    setTaskHistory(TASK_ID, [
      {
        type: "observation_delta",
        content: "obs one",
        metadata: {
          id: "turn-3",
          ind: 3,
          turn_sequence: 3,
          sequence: 30,
          step_type: "observation_delta",
          sub_turn_index: 0,
        },
      },
      {
        type: "observation_delta",
        content: "obs two",
        metadata: {
          id: "turn-3",
          ind: 3,
          turn_sequence: 3,
          sequence: 31,
          step_type: "observation_delta",
          sub_turn_index: 1,
        },
      },
    ]);

    const items = snapshotItems().filter((item) => item.type === "observation_delta");
    expect(items).toHaveLength(2);
    expect(items.map((item) => item.content)).toEqual(["obs one", "obs two"]);
  });

  it("keeps canonical alternating order for repeated tool/observation cycles on history replay", () => {
    setTaskHistory(TASK_ID, [
      {
        type: "observation_delta",
        content: "observation two",
        metadata: {
          id: "turn-6",
          ind: 1,
          turn_sequence: 6,
          sequence: 64,
          step_type: "observation_delta",
          sub_turn_index: 1,
        },
        timestamp: "2026-03-01T10:00:09.000Z",
      },
      {
        type: "tool_start",
        content: "",
        metadata: {
          id: "turn-6",
          ind: 1,
          turn_sequence: 6,
          sequence: 63,
          step_type: "tool_start",
          tool_call_id: "call-2",
          sub_turn_index: 1,
        },
        timestamp: "2026-03-01T10:00:10.000Z",
      },
      {
        type: "observation_delta",
        content: "observation one",
        metadata: {
          id: "turn-6",
          ind: 1,
          turn_sequence: 6,
          sequence: 62,
          step_type: "observation_delta",
          sub_turn_index: 0,
        },
        timestamp: "2026-03-01T10:00:07.000Z",
      },
      {
        type: "tool_start",
        content: "",
        metadata: {
          id: "turn-6",
          ind: 1,
          turn_sequence: 6,
          sequence: 61,
          step_type: "tool_start",
          tool_call_id: "call-1",
          sub_turn_index: 0,
        },
        timestamp: "2026-03-01T10:00:08.000Z",
      },
    ]);

    const itemOrder = snapshotItems()
      .filter((item) => item.type === "tool_start" || item.type === "observation_delta")
      .map((item) => ({
        type: item.type,
        sequence: (item.metadata as Record<string, unknown>)?.sequence,
      }));

    expect(itemOrder).toEqual([
      { type: "tool_start", sequence: 61 },
      { type: "observation_delta", sequence: 62 },
      { type: "tool_start", sequence: 63 },
      { type: "observation_delta", sequence: 64 },
    ]);
  });

  it("resetTaskStreamForResync clears items and keeps cursor monotonic", () => {
    setTaskHistory(TASK_ID, [
      {
        type: "reasoning_delta",
        content: "before-resync",
        metadata: {
          id: "turn-4",
          ind: 0,
          turn_sequence: 4,
          sequence: 40,
          step_type: "reasoning_delta",
          reasoning_section_id: "turn-4:reasoning:0",
          phase_sequence: 0,
        },
      },
    ]);

    resetTaskStreamForResync(TASK_ID, 41);

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.items).toHaveLength(0);
    expect(snapshot.historyLoaded).toBe(false);
    expect(snapshot.lastSequence).toBe(41);
  });

  it("rehydrates history after resync reset", () => {
    setTaskHistory(TASK_ID, [
      {
        type: "reasoning_delta",
        content: "stale",
        metadata: {
          id: "turn-5",
          ind: 0,
          turn_sequence: 5,
          sequence: 50,
          step_type: "reasoning_delta",
          reasoning_section_id: "turn-5:reasoning:0",
          phase_sequence: 0,
        },
      },
    ]);

    resetTaskStreamForResync(TASK_ID, 50);
    setTaskHistory(TASK_ID, [
      {
        type: "observation_delta",
        content: "fresh-history",
        metadata: {
          id: "turn-5",
          ind: 1,
          turn_sequence: 5,
          sequence: 51,
          step_type: "observation_delta",
          sub_turn_index: 0,
        },
      },
    ]);

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.historyLoaded).toBe(true);
    expect(snapshot.items.map((item) => item.content)).toEqual(["fresh-history"]);
    expect(snapshot.lastSequence).toBe(51);
  });

  it("tracks history readiness by conversation identity", () => {
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-a")).toBe(false);
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-b")).toBe(false);

    setHistoryLoaded(TASK_ID, "conv-a");

    expect(hasConversationHistoryLoaded(TASK_ID, "conv-a")).toBe(true);
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-b")).toBe(false);

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.historyLoaded).toBe(true);
    expect(snapshot.historyLoadedByConversation).toEqual({
      "conv-a": true,
    });
  });

  it("clears conversation readiness markers during resync reset", () => {
    setHistoryLoaded(TASK_ID, "conv-a");
    setHistoryLoaded(TASK_ID, "conv-b");
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-a")).toBe(true);
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-b")).toBe(true);

    resetTaskStreamForResync(TASK_ID, 100);

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.historyLoaded).toBe(false);
    expect(snapshot.historyLoadedByConversation).toEqual({});
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-a")).toBe(false);
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-b")).toBe(false);
  });

  it("tracks history loading markers by conversation and clears them on completion", () => {
    expect(isConversationHistoryLoading(TASK_ID, "conv-a")).toBe(false);
    setHistoryLoading(TASK_ID, true, "conv-a");
    expect(isConversationHistoryLoading(TASK_ID, "conv-a")).toBe(true);

    setHistoryLoaded(TASK_ID, "conv-a");

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(isConversationHistoryLoading(TASK_ID, "conv-a")).toBe(false);
    expect(snapshot.historyLoadingByConversation).toEqual({});
    expect(snapshot.historyLoadedByConversation).toEqual({ "conv-a": true });
  });

  it("blocks duplicate loading intent while loading and resets marker when unset", () => {
    expect(isConversationHistoryLoading(TASK_ID, "conv-z")).toBe(false);
    setHistoryLoading(TASK_ID, true, "conv-z");
    setHistoryLoading(TASK_ID, true, "conv-z");
    expect(isConversationHistoryLoading(TASK_ID, "conv-z")).toBe(true);

    let snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.historyLoadingByConversation).toEqual({ "conv-z": true });

    setHistoryLoading(TASK_ID, false, "conv-z");
    expect(isConversationHistoryLoading(TASK_ID, "conv-z")).toBe(false);

    snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.historyLoadingByConversation).toEqual({});
  });

  it("clears loading markers during resync reset", () => {
    setHistoryLoading(TASK_ID, true, "conv-a");
    expect(isConversationHistoryLoading(TASK_ID, "conv-a")).toBe(true);

    resetTaskStreamForResync(TASK_ID, 200);

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(isConversationHistoryLoading(TASK_ID, "conv-a")).toBe(false);
    expect(snapshot.historyLoadingByConversation).toEqual({});
  });

  it("persists transcript pagination cursor state per conversation", () => {
    setTranscriptPaginationState(TASK_ID, {
      conversationId: "conv-page",
      hasMoreOlder: true,
      nextBeforeCursor: 55,
    });

    expect(getConversationHasMoreOlder(TASK_ID, "conv-page")).toBe(true);
    expect(getConversationNextBeforeCursor(TASK_ID, "conv-page")).toBe(55);

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.hasMoreOlderByConversation).toEqual({ "conv-page": true });
    expect(snapshot.nextBeforeCursorByConversation).toEqual({ "conv-page": 55 });
  });

  it("coerces invalid before cursor and still persists hasMore flag", () => {
    setTranscriptPaginationState(TASK_ID, {
      conversationId: "conv-invalid",
      hasMoreOlder: false,
      nextBeforeCursor: -4,
    });

    expect(getConversationHasMoreOlder(TASK_ID, "conv-invalid")).toBe(false);
    expect(getConversationNextBeforeCursor(TASK_ID, "conv-invalid")).toBe(null);
  });

  it("keeps pagination cursor monotonic across repeated older-page merges", () => {
    setTranscriptPaginationState(TASK_ID, {
      conversationId: "conv-monotonic",
      hasMoreOlder: true,
      nextBeforeCursor: 55,
    });
    setTranscriptPaginationState(TASK_ID, {
      conversationId: "conv-monotonic",
      hasMoreOlder: true,
      nextBeforeCursor: 80,
    });

    expect(getConversationHasMoreOlder(TASK_ID, "conv-monotonic")).toBe(true);
    expect(getConversationNextBeforeCursor(TASK_ID, "conv-monotonic")).toBe(55);

    setTranscriptPaginationState(TASK_ID, {
      conversationId: "conv-monotonic",
      hasMoreOlder: true,
      nextBeforeCursor: 40,
    });

    expect(getConversationNextBeforeCursor(TASK_ID, "conv-monotonic")).toBe(40);
  });

  it("clears cursor when backend marks no more older pages", () => {
    setTranscriptPaginationState(TASK_ID, {
      conversationId: "conv-end",
      hasMoreOlder: true,
      nextBeforeCursor: 33,
    });
    setTranscriptPaginationState(TASK_ID, {
      conversationId: "conv-end",
      hasMoreOlder: false,
      nextBeforeCursor: 22,
    });

    expect(getConversationHasMoreOlder(TASK_ID, "conv-end")).toBe(false);
    expect(getConversationNextBeforeCursor(TASK_ID, "conv-end")).toBe(null);
  });

  it("blocks duplicate startup ownership via store-level loading claim", () => {
    expect(tryStartHistoryLoading(TASK_ID, "conv-owner")).toBe(true);
    expect(tryStartHistoryLoading(TASK_ID, "conv-owner")).toBe(false);
    expect(isConversationHistoryLoading(TASK_ID, "conv-owner")).toBe(true);
  });

  it("marks bootstrap terminal conversation and clears loading lock", () => {
    setHistoryLoading(TASK_ID, true, "conv-missing");
    expect(isConversationHistoryLoading(TASK_ID, "conv-missing")).toBe(true);

    markHistoryBootstrapTerminal(TASK_ID, "conv-missing");

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(isConversationHistoryLoading(TASK_ID, "conv-missing")).toBe(false);
    expect(isHistoryBootstrapTerminal(TASK_ID, "conv-missing")).toBe(true);
    expect(snapshot.historyBootstrapTerminalByConversation).toEqual({ "conv-missing": true });
  });

  it("clears bootstrap terminal marker after successful history load", () => {
    markHistoryBootstrapTerminal(TASK_ID, "conv-recovered");
    expect(isHistoryBootstrapTerminal(TASK_ID, "conv-recovered")).toBe(true);

    setHistoryLoaded(TASK_ID, "conv-recovered");

    expect(isHistoryBootstrapTerminal(TASK_ID, "conv-recovered")).toBe(false);
    expect(hasConversationHistoryLoaded(TASK_ID, "conv-recovered")).toBe(true);
  });

  it("supports explicit terminal clear for manual retry flows", () => {
    markHistoryBootstrapTerminal(TASK_ID, "conv-retry");
    expect(isHistoryBootstrapTerminal(TASK_ID, "conv-retry")).toBe(true);

    clearHistoryBootstrapTerminal(TASK_ID, "conv-retry");

    expect(isHistoryBootstrapTerminal(TASK_ID, "conv-retry")).toBe(false);
  });
});
