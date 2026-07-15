import { afterEach, describe, expect, it } from "vitest";

import {
  clearTaskState,
  getLastSequence,
  getTaskStreamSnapshot,
} from "@/state/chat-stream-store";
import { groupMessages } from "@/hooks/useMessageGrouping";
import type { RuntimeAgentReasoningEnvelope } from "../types";
import { StreamPacketIngestor } from "../StreamPacketIngestor";

const TASK_ID = 88001;

function envelope(packet: Record<string, unknown>, sequence = 10): RuntimeAgentReasoningEnvelope {
  return {
    type: "agent_reasoning",
    taskId: TASK_ID,
    sequence,
    packet,
  };
}

afterEach(() => {
  clearTaskState(TASK_ID);
});

describe("StreamPacketIngestor", () => {
  it("ingests non-status stream packet events into task store", () => {
    const ingestor = new StreamPacketIngestor();

    const ok = ingestor.ingestEnvelope(
      envelope({
        placement: { turn_index: 1, tab_index: 1 },
        obj: {
          type: "tool_start",
          content: "running tool",
          metadata: { id: "turn-1", ind: 1, turn_sequence: 1, step_type: "tool_start" },
        },
      }),
    );

    expect(ok).toBe(true);
    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.items.map((item) => item.type)).toContain("tool_start");
  });

  it("ingests status events and keeps cursor monotonic", () => {
    const ingestor = new StreamPacketIngestor();

    ingestor.ingestEnvelope(
      envelope(
        {
          type: "status",
          content: "run_state",
          metadata: { sequence: 5, state: "running" },
        },
        11,
      ),
    );

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.items.map((item) => item.type)).toContain("status");
    expect(getLastSequence(TASK_ID)).toBe(11);
  });

  it("accepts plan control packets without inserting transcript rows", () => {
    const ingestor = new StreamPacketIngestor();

    const ok = ingestor.ingestEnvelope(
      envelope(
        {
          placement: { turn_index: 1 },
          obj: {
            type: "todo_progress",
            content: "",
            metadata: {
              id: "turn-1",
              turn_sequence: 1,
              step_type: "todo_progress",
              streaming: false,
            },
            todo_updates: [{ id: "1", status: "completed" }],
          },
        },
        12,
      ),
    );

    expect(ok).toBe(true);
    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.items).toHaveLength(0);
    expect(getLastSequence(TASK_ID)).toBe(12);
  });

  it("accepts direct plan control events without inserting transcript rows", () => {
    const ingestor = new StreamPacketIngestor();

    const ok = ingestor.ingestEnvelope(
      envelope(
        {
          type: "plan_created",
          content: "",
          metadata: {
            id: "turn-1",
            turn_sequence: 1,
            step_type: "plan_created",
            streaming: false,
          },
          plan_steps: ["Find live hosts"],
        },
        13,
      ),
    );

    expect(ok).toBe(true);
    const snapshot = getTaskStreamSnapshot(TASK_ID);
    expect(snapshot.items).toHaveLength(0);
    expect(getLastSequence(TASK_ID)).toBe(13);
  });

  it("backfills task and sequence metadata from envelope", () => {
    const ingestor = new StreamPacketIngestor();

    ingestor.ingestEnvelope(
      envelope(
        {
          type: "reasoning_delta",
          content: "thinking",
          metadata: {
            id: "turn-2",
            ind: 0,
            step_type: "reasoning_delta",
            reasoning_section_id: "turn-2:reasoning:0",
            phase_sequence: 0,
          },
        },
        42,
      ),
    );

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    const reasoning = snapshot.items.find((item) => item.type === "reasoning_delta");
    expect(reasoning).toBeTruthy();
    expect(reasoning?.sequence).toBe(42);
    expect(reasoning?.task_id).toBe(TASK_ID);
    expect(getLastSequence(TASK_ID)).toBe(42);
  });

  it("keeps live interleaved reasoning after observations in stream order", () => {
    const ingestor = new StreamPacketIngestor();

    const ingestPacket = (
      sequence: number,
      type: string,
      metadata: Record<string, unknown>,
      content = "",
    ) => {
      ingestor.ingestEnvelope(
        envelope(
          {
            placement: { turn_index: 10, tab_index: metadata.ind },
            obj: {
              type,
              content,
              metadata: {
                id: "turn-live",
                turn_sequence: 10,
                step_type: type,
                ...metadata,
              },
            },
          },
          sequence,
        ),
      );
    };

    ingestPacket(1, "reasoning_start", {
      ind: 0,
      sub_turn_index: 0,
      reasoning_section_id: "turn-live:reasoning:0",
      phase_sequence: 0,
    });
    ingestPacket(2, "reasoning_delta", {
      ind: 0,
      sub_turn_index: 0,
      reasoning_section_id: "turn-live:reasoning:0",
      phase_sequence: 0,
    }, "initial thought");
    ingestPacket(3, "reasoning_section_end", {
      ind: 0,
      sub_turn_index: 0,
      reasoning_section_id: "turn-live:reasoning:0",
      phase_sequence: 0,
    });
    ingestPacket(4, "tool_start", { ind: 1, id: "tool-1", tool_call_id: "tc-1" });
    ingestPacket(5, "tool_end", { ind: 1, id: "tool-1", tool_call_id: "tc-1" });
    ingestPacket(6, "observation_delta", { ind: 1, id: "obs-1" }, "first observation");
    ingestPacket(7, "observation_section_end", { ind: 1, id: "obs-1" });
    ingestPacket(8, "reasoning_start", {
      ind: 0,
      sub_turn_index: 1,
      reasoning_section_id: "turn-live:reasoning:3",
      phase_sequence: 3,
    });
    ingestPacket(9, "reasoning_delta", {
      ind: 0,
      sub_turn_index: 1,
      reasoning_section_id: "turn-live:reasoning:3",
      phase_sequence: 3,
    }, "post-observation thought");
    ingestPacket(10, "reasoning_section_end", {
      ind: 0,
      sub_turn_index: 1,
      reasoning_section_id: "turn-live:reasoning:3",
      phase_sequence: 3,
    });
    ingestPacket(11, "tool_start", { ind: 1, id: "tool-2", tool_call_id: "tc-2" });
    ingestPacket(12, "tool_end", { ind: 1, id: "tool-2", tool_call_id: "tc-2" });
    ingestPacket(13, "observation_delta", { ind: 1, id: "obs-2" }, "second observation");

    const snapshot = getTaskStreamSnapshot(TASK_ID);
    const groups = groupMessages(snapshot.items as any);

    expect(groups.map((group) => group.primaryType)).toEqual([
      "reasoning",
      "tool",
      "observation",
      "reasoning",
      "tool",
      "observation",
    ]);
    expect(groups.map((group) => group.messages[0].metadata?.sequence)).toEqual([
      1,
      4,
      6,
      8,
      11,
      13,
    ]);
  });

  it("does not merge separate live reasoning sections without sub-turn indexes", () => {
    const ingestor = new StreamPacketIngestor();

    const ingestReasoning = (
      sequence: number,
      type: "reasoning_start" | "reasoning_delta" | "reasoning_section_end",
      reasoningSectionId: string,
      phaseSequence: number,
      content = "",
    ) => {
      ingestor.ingestEnvelope(
        envelope(
          {
            placement: { turn_index: 20, tab_index: 0 },
            obj: {
              type,
              content,
              metadata: {
                id: "turn-merge",
                ind: 0,
                turn_sequence: 20,
                step_type: type,
                reasoning_section_id: reasoningSectionId,
                phase_sequence: phaseSequence,
              },
            },
          },
          sequence,
        ),
      );
    };

    ingestReasoning(1, "reasoning_start", "turn-merge:reasoning:0", 0);
    ingestReasoning(2, "reasoning_delta", "turn-merge:reasoning:0", 0, "Analyzing request and creating a plan.");
    ingestReasoning(3, "reasoning_section_end", "turn-merge:reasoning:0", 0);
    ingestReasoning(4, "reasoning_start", "turn-merge:reasoning:1", 1);
    ingestReasoning(5, "reasoning_delta", "turn-merge:reasoning:1", 1, "Selecting relevant tool categories.");
    ingestReasoning(6, "reasoning_section_end", "turn-merge:reasoning:1", 1);
    ingestReasoning(7, "reasoning_start", "turn-merge:reasoning:2", 2);
    ingestReasoning(8, "reasoning_delta", "turn-merge:reasoning:2", 2, "Preparing tool execution.");
    ingestReasoning(9, "reasoning_section_end", "turn-merge:reasoning:2", 2);

    const groups = groupMessages(getTaskStreamSnapshot(TASK_ID).items as any);

    expect(groups).toHaveLength(3);
    expect(groups.map((group) => group.primaryType)).toEqual([
      "reasoning",
      "reasoning",
      "reasoning",
    ]);
    expect(groups.map((group) => group.messages.map((message) => message.content).join(""))).toEqual([
      "Analyzing request and creating a plan.",
      "Selecting relevant tool categories.",
      "Preparing tool execution.",
    ]);
  });
});
