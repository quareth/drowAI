import { describe, expect, it } from "vitest";

import type { ChatMessage } from "@/components/chat/types";
import {
  buildMessageRenderBlocks,
  type MessageRenderBlock,
  type TurnActivityBlock,
} from "@/components/chat/turnActivityBlocks";
import { groupMessages } from "@/hooks/useMessageGrouping";

function makeMessage(
  id: string,
  stepType: string,
  metadata: Record<string, unknown> = {},
): ChatMessage {
  const turnSequence = metadata.turn_sequence ?? 1;
  const reasoningMetadata = stepType.startsWith("reasoning")
    ? {
        reasoning_section_id:
          metadata.reasoning_section_id ??
          `turn-${turnSequence}:reasoning:${metadata.sub_turn_index ?? 0}`,
      }
    : {};

  return {
    id,
    type: "agent",
    content: "",
    timestamp: "2024-01-01T00:00:00Z",
    isStreaming: Boolean(metadata.streaming),
    metadata: {
      step_type: stepType,
      turn_sequence: 1,
      id: "turn-1",
      ...reasoningMetadata,
      ...metadata,
    },
  };
}

function buildBlocks(messages: ChatMessage[]): MessageRenderBlock[] {
  return buildMessageRenderBlocks(groupMessages(messages));
}

function activityBlock(blocks: MessageRenderBlock[]): TurnActivityBlock {
  const block = blocks.find((candidate) => candidate.type === "activity");
  if (!block || block.type !== "activity") {
    throw new Error("Expected an activity block");
  }
  return block;
}

describe("buildMessageRenderBlocks", () => {
  it("coalesces adjacent reasoning sections in a live turn", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning-1-start", "reasoning_start", {
        ind: 0,
        sub_turn_index: 0,
      }),
      makeMessage("reasoning-1-delta", "reasoning_delta", {
        ind: 0,
        sub_turn_index: 0,
      }),
      makeMessage("reasoning-1-end", "reasoning_section_end", {
        ind: 0,
        sub_turn_index: 0,
      }),
      makeMessage("reasoning-2-start", "reasoning_start", {
        ind: 0,
        sub_turn_index: 1,
      }),
      makeMessage("reasoning-2-delta", "reasoning_delta", {
        ind: 0,
        sub_turn_index: 1,
        streaming: true,
      }),
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe("activity");
    if (blocks[0].type !== "activity") throw new Error("Expected live activity");
    expect(blocks[0].isComplete).toBe(false);
    expect(blocks[0].groups).toHaveLength(1);
    expect(blocks[0].groups[0].primaryType).toBe("reasoning");
    expect(blocks[0].groups[0].messages.map((message) => message.id)).toEqual([
      "reasoning-1-start",
      "reasoning-1-delta",
      "reasoning-1-end",
      "reasoning-2-start",
      "reasoning-2-delta",
    ]);
  });

  it("keeps reasoning bursts separate across a visible tool boundary", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning-1", "reasoning_delta", {
        ind: 0,
        sub_turn_index: 0,
        sequence: 1,
      }),
      makeMessage("tool", "tool_start", {
        ind: 1,
        tool_call_id: "tc-live",
        streaming: true,
        sequence: 2,
      }),
      makeMessage("reasoning-2", "reasoning_delta", {
        ind: 0,
        sub_turn_index: 1,
        streaming: true,
        sequence: 3,
      }),
    ]);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe("activity");
    if (blocks[0].type !== "activity") throw new Error("Expected live activity");
    expect(blocks[0].groups.map((group) => group.primaryType)).toEqual([
      "reasoning",
      "tool",
      "reasoning",
    ]);
  });

  it("coalesces reflection, think-more, and future reasoning in completed details", () => {
    const reasoningMessages = ["reflection", "reasoning_loop", "future_phase"].flatMap(
      (sectionName, index) => [
        makeMessage(`${sectionName}-start`, "reasoning_start", {
          ind: 0,
          sub_turn_index: index,
          section_name: sectionName,
        }),
        makeMessage(`${sectionName}-delta`, "reasoning_delta", {
          ind: 0,
          sub_turn_index: index,
          section_name: sectionName,
        }),
        makeMessage(`${sectionName}-end`, "reasoning_section_end", {
          ind: 0,
          sub_turn_index: index,
          section_name: sectionName,
        }),
      ],
    );
    const blocks = buildBlocks([
      ...reasoningMessages,
      makeMessage("final", "message_delta", {
        ind: 2,
        final_snapshot: true,
      }),
    ]);

    const activity = activityBlock(blocks);
    const reasoningGroups = activity.groups.filter(
      (group) => group.primaryType === "reasoning",
    );
    expect(activity.summary.thoughtCount).toBe(1);
    expect(reasoningGroups).toHaveLength(1);
    expect(
      reasoningGroups[0].messages
        .filter((message) => message.metadata?.step_type === "reasoning_start")
        .map((message) => message.metadata?.section_name),
    ).toEqual(["reflection", "reasoning_loop", "future_phase"]);
  });

  it("groups live incomplete turn activity into the shared chain block", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning", "reasoning_delta", { ind: 0, streaming: true }),
      makeMessage("tool", "tool_start", {
        ind: 1,
        tool_call_id: "tc-live",
        tool: "nmap",
        streaming: true,
      }),
    ]);

    expect(blocks.map((block) => block.type)).toEqual(["activity"]);
    const activity = activityBlock(blocks);
    expect(activity.isComplete).toBe(false);
    expect(activity.groups.map((group) => group.primaryType)).toEqual([
      "reasoning",
      "tool",
    ]);
  });

  it("collapses completed turn activity before the final answer", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning-start", "reasoning_start", { ind: 0 }),
      makeMessage("reasoning-delta", "reasoning_delta", { ind: 0 }),
      makeMessage("reasoning-end", "reasoning_section_end", { ind: 0 }),
      makeMessage("tool-start", "tool_start", {
        ind: 1,
        tool_call_id: "tc-1",
        tool: "nmap",
      }),
      makeMessage("tool-end", "tool_end", {
        ind: 1,
        tool_call_id: "tc-1",
        tool: "nmap",
        status: "success",
      }),
      makeMessage("obs-delta", "observation_delta", {
        ind: 1,
        id: "obs-1",
        content: "observed",
      }),
      makeMessage("obs-end", "observation_section_end", {
        ind: 1,
        id: "obs-1",
      }),
      makeMessage("message-start", "message_start", {
        ind: 2,
        streaming: true,
      }),
      makeMessage("final", "message_delta", {
        ind: 2,
        final_snapshot: true,
      }),
    ]);

    expect(blocks.map((block) => block.type)).toEqual(["activity", "group"]);
    expect(activityBlock(blocks).isComplete).toBe(true);
    expect(activityBlock(blocks).summary).toEqual({
      thoughtCount: 1,
      toolCount: 1,
      agentCount: 0,
      observationCount: 1,
    });
    expect(blocks[1].type === "group" ? blocks[1].group.primaryType : "").toBe("message");
  });

  it("keeps an agent-run handoff inside the ordered completed-turn activity", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning", "reasoning_delta", {
        ind: 0,
        sequence: 10,
      }),
      makeMessage("reasoning-end", "reasoning_section_end", {
        ind: 0,
        sequence: 11,
      }),
      makeMessage("scout", "tool_start", {
        ind: 1,
        sequence: 12,
        subtype: "agent_run_lifecycle",
        agent_run_id: "scout-run-1",
      }),
      makeMessage("final", "message_delta", {
        ind: 2,
        sequence: 20,
        final_snapshot: true,
      }),
    ]);

    expect(blocks.map((block) => block.type)).toEqual(["activity", "group"]);
    const activity = activityBlock(blocks);
    expect(activity.groups.map((group) => group.messages[0]?.id)).toEqual([
      "reasoning",
      "scout",
    ]);
    expect(activity.summary).toEqual({
      thoughtCount: 1,
      toolCount: 0,
      agentCount: 1,
      observationCount: 0,
    });
  });

  it("preserves distinct subagent events between the reasoning steps that launched them", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning-1", "reasoning_delta", {
        ind: 0,
        sequence: 10,
        sub_turn_index: 0,
      }),
      makeMessage("reasoning-1-end", "reasoning_section_end", {
        ind: 0,
        sequence: 11,
        sub_turn_index: 0,
      }),
      makeMessage("researcher", "tool_start", {
        ind: 1,
        sequence: 12,
        subtype: "agent_run_lifecycle",
        agent_run_id: "research-run-1",
        agent_id: "pathfinder",
        agent_kind: "research",
        agent_display_name: "Researcher",
      }),
      makeMessage("reasoning-2", "reasoning_delta", {
        ind: 0,
        sequence: 13,
        sub_turn_index: 1,
      }),
      makeMessage("reasoning-2-end", "reasoning_section_end", {
        ind: 0,
        sequence: 14,
        sub_turn_index: 1,
      }),
      makeMessage("reviewer", "tool_start", {
        ind: 1,
        sequence: 15,
        subtype: "agent_run_lifecycle",
        agent_run_id: "review-run-1",
        agent_id: "pathfinder",
        agent_kind: "review",
        agent_display_name: "Reviewer",
      }),
      makeMessage("observation", "observation_delta", {
        ind: 1,
        sequence: 16,
        id: "observation-1",
      }),
      makeMessage("final", "message_delta", {
        ind: 2,
        sequence: 20,
        final_snapshot: true,
      }),
    ]);

    expect(blocks.map((block) => block.type)).toEqual(["activity", "group"]);
    expect(
      activityBlock(blocks).groups.map((group) => group.messages[0]?.id),
    ).toEqual([
      "reasoning-1",
      "researcher",
      "reasoning-2",
      "reviewer",
      "observation",
    ]);
    expect(activityBlock(blocks).summary.agentCount).toBe(2);
  });

  it("counts each distinct generic subagent run in the completed-turn summary", () => {
    const agentKinds = ["research", "review", "explore", "execute", "verify"];
    const blocks = buildBlocks([
      makeMessage("reasoning", "reasoning_delta", {
        ind: 0,
        sequence: 10,
      }),
      ...agentKinds.map((agentKind, index) =>
        makeMessage(`agent-${index + 1}`, "tool_start", {
          ind: 1,
          sequence: 11 + index,
          subtype: "agent_run_lifecycle",
          agent_run_id: `${agentKind}-run-${index + 1}`,
          agent_id: "pathfinder",
          agent_kind: agentKind,
          agent_display_name: `Agent ${index + 1}`,
        }),
      ),
      makeMessage("final", "message_delta", {
        ind: 2,
        sequence: 20,
        final_snapshot: true,
      }),
    ]);

    expect(activityBlock(blocks).summary.agentCount).toBe(5);
  });

  it("collapses history-replayed assistant turns", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning-delta", "reasoning_delta", {
        ind: 0,
        sequence_authority: "canonical_detail",
      }),
      makeMessage("reasoning-end", "reasoning_section_end", {
        ind: 0,
        sequence_authority: "canonical_detail",
      }),
      makeMessage("assistant", "assistant_message", {
        ind: 2,
        sequence_authority: "synthetic_message",
      }),
    ]);

    expect(blocks.map((block) => block.type)).toEqual(["activity", "group"]);
    expect(activityBlock(blocks).summary.thoughtCount).toBe(1);
  });

  it("counts distinct tool calls within one batch group", () => {
    const blocks = buildBlocks([
      makeMessage("tool-a-start", "tool_start", {
        ind: 1,
        tool_batch_id: "tb-1",
        tool_call_id: "tc-a",
      }),
      makeMessage("tool-b-start", "tool_start", {
        ind: 1,
        tool_batch_id: "tb-1",
        tool_call_id: "tc-b",
      }),
      makeMessage("tool-a-end", "tool_end", {
        ind: 1,
        tool_batch_id: "tb-1",
        tool_call_id: "tc-a",
        status: "success",
      }),
      makeMessage("tool-b-end", "tool_end", {
        ind: 1,
        tool_batch_id: "tb-1",
        tool_call_id: "tc-b",
        status: "success",
      }),
      makeMessage("final", "message_delta", {
        ind: 2,
        final_snapshot: true,
      }),
    ]);

    const activity = activityBlock(blocks);
    expect(activity.summary.toolCount).toBe(2);
    expect(activity.groups.filter((group) => group.primaryType === "tool")).toHaveLength(1);
  });

  it("does not collapse error turns", () => {
    const blocks = buildBlocks([
      makeMessage("reasoning", "reasoning_delta", { ind: 0 }),
      makeMessage("assistant-error", "assistant_message", {
        ind: 2,
        status: "error",
      }),
    ]);

    expect(blocks.map((block) => block.type)).toEqual(["activity", "group"]);
    if (blocks[0].type !== "activity") throw new Error("Expected live activity");
    expect(blocks[0].isComplete).toBe(false);
  });
});
