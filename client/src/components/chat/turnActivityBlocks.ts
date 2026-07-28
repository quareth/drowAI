/**
 * Builds live and completed-turn render blocks from chat message groups.
 *
 * The helper keeps the stream contract untouched: adjacent same-turn reasoning
 * sections render as one card in live and completed details, while completed
 * turns place all intermediate activity behind one expandable row.
 */

import type { MessageGroup } from "@/hooks/useMessageGrouping";
import type { ChatMessage } from "./types";

export interface TurnActivitySummary {
  thoughtCount: number;
  toolCount: number;
  agentCount: number;
  observationCount: number;
}

export interface NormalMessageBlock {
  type: "group";
  key: string;
  group: MessageGroup;
}

export interface TurnActivityBlock {
  type: "activity";
  key: string;
  turnKey: string;
  isComplete: boolean;
  groups: MessageGroup[];
  summary: TurnActivitySummary;
}

export type MessageRenderBlock = NormalMessageBlock | TurnActivityBlock;

function coerceTurnValue(value: unknown): string | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  if (typeof value === "string" && value.trim().length > 0) {
    return value.trim();
  }
  return undefined;
}

function getStepType(message: ChatMessage): string | undefined {
  const stepType = message.metadata?.step_type ?? message.metadata?.stepType;
  return typeof stepType === "string" ? stepType : undefined;
}

function isStreaming(message: ChatMessage): boolean {
  const metadata = message.metadata ?? {};
  return Boolean(
    message.isStreaming ||
      metadata.streaming ||
      metadata.is_streaming ||
      metadata.in_progress,
  );
}

export function resolveTurnKey(group: MessageGroup): string | undefined {
  for (const message of group.messages) {
    const metadata = message.metadata ?? {};
    const turnSequence = coerceTurnValue(metadata.turn_sequence);
    if (turnSequence) return `turn-sequence:${turnSequence}`;

    const turnId = coerceTurnValue(metadata.turn_id ?? metadata.id);
    if (turnId) return `turn-id:${turnId}`;
  }
  return undefined;
}

function hasSuccessfulFinalMessage(group: MessageGroup): boolean {
  if (group.primaryType !== "message") return false;

  let hasFinal = false;
  let hasOpenContentStream = false;
  for (const message of group.messages) {
    if (message.metadata?.status === "error") return false;

    const stepType = getStepType(message);
    if (stepType === "assistant_message" && !isStreaming(message)) {
      hasFinal = true;
    } else if (stepType === "message_delta" && message.metadata?.final_snapshot === true) {
      hasFinal = !isStreaming(message);
    } else if (
      (stepType === "message_delta" || stepType === "assistant_delta") &&
      isStreaming(message)
    ) {
      hasOpenContentStream = true;
    }
  }
  return hasFinal && !hasOpenContentStream;
}

export function isAgentRunEventGroup(group: MessageGroup): boolean {
  return group.messages.some((message) => {
    const metadata = message.metadata ?? {};
    const hasRunId =
      typeof metadata.agent_run_id === "string" &&
      metadata.agent_run_id.trim().length > 0;
    if (!hasRunId) {
      return false;
    }
    const isLifecycle =
      message.content === "agent_run_lifecycle" ||
      metadata.subtype === "agent_run_lifecycle";
    if (isLifecycle) {
      return true;
    }
    return (
      metadata.producer_type !== "subagent" &&
      typeof metadata.agent_kind === "string" &&
      metadata.agent_kind.trim().length > 0 &&
      typeof metadata.agent_display_name === "string" &&
      metadata.agent_display_name.trim().length > 0
    );
  });
}

function isCollapsibleIntermediate(group: MessageGroup): boolean {
  return (
    isAgentRunEventGroup(group) ||
    group.primaryType === "reasoning" ||
    group.primaryType === "tool" ||
    group.primaryType === "observation"
  );
}

function collectToolIds(group: MessageGroup, ids: Set<string>): void {
  for (const message of group.messages) {
    const metadata = message.metadata ?? {};
    const directId = metadata.tool_call_id;
    if (typeof directId === "string" && directId.trim().length > 0) {
      ids.add(directId.trim());
    }

    for (const key of ["tool_calls", "calls", "results"]) {
      const entries = metadata[key];
      if (!Array.isArray(entries)) continue;
      for (const entry of entries) {
        if (!entry || typeof entry !== "object") continue;
        const callId = (entry as Record<string, unknown>).tool_call_id;
        if (typeof callId === "string" && callId.trim().length > 0) {
          ids.add(callId.trim());
        }
      }
    }
  }
}

function collectAgentRunIds(group: MessageGroup, ids: Set<string>): void {
  for (const message of group.messages) {
    const agentRunId = message.metadata?.agent_run_id;
    if (typeof agentRunId === "string" && agentRunId.trim().length > 0) {
      ids.add(agentRunId.trim());
    }
  }
}

export function summarizeActivityGroups(groups: MessageGroup[]): TurnActivitySummary {
  const toolIds = new Set<string>();
  const agentRunIds = new Set<string>();
  let toolGroupsWithoutIds = 0;
  let thoughtCount = 0;
  let observationCount = 0;

  for (const group of groups) {
    if (isAgentRunEventGroup(group)) {
      collectAgentRunIds(group, agentRunIds);
      continue;
    }
    if (group.primaryType === "reasoning") {
      thoughtCount += 1;
    } else if (group.primaryType === "observation") {
      observationCount += 1;
    } else if (group.primaryType === "tool") {
      const before = toolIds.size;
      collectToolIds(group, toolIds);
      if (toolIds.size === before) {
        toolGroupsWithoutIds += 1;
      }
    }
  }

  return {
    thoughtCount,
    toolCount: toolIds.size + toolGroupsWithoutIds,
    agentCount: agentRunIds.size,
    observationCount,
  };
}

function completedTurnKeys(groups: MessageGroup[]): Set<string> {
  const keys = new Set<string>();
  for (const group of groups) {
    if (!hasSuccessfulFinalMessage(group)) continue;
    const turnKey = resolveTurnKey(group);
    if (turnKey) keys.add(turnKey);
  }
  return keys;
}

function normalBlock(group: MessageGroup, index: number): NormalMessageBlock {
  return {
    type: "group",
    key: group.key ?? `group-${group.ind}-${index}`,
    group,
  };
}

function coalesceAdjacentReasoningGroups(groups: MessageGroup[]): MessageGroup[] {
  const coalesced: MessageGroup[] = [];

  for (const group of groups) {
    const previous = coalesced[coalesced.length - 1];
    const turnKey = resolveTurnKey(group);
    if (
      group.primaryType === "reasoning" &&
      previous?.primaryType === "reasoning" &&
      turnKey !== undefined &&
      turnKey === resolveTurnKey(previous)
    ) {
      coalesced[coalesced.length - 1] = {
        ...previous,
        messages: [...previous.messages, ...group.messages],
      };
      continue;
    }

    coalesced.push(group);
  }

  return coalesced;
}

export function buildMessageRenderBlocks(groups: MessageGroup[]): MessageRenderBlock[] {
  const renderGroups = coalesceAdjacentReasoningGroups(groups);
  const completedTurns = completedTurnKeys(renderGroups);
  const blocks: MessageRenderBlock[] = [];
  let pendingTurnKey: string | undefined;
  let pendingGroups: MessageGroup[] = [];

  const flushPending = () => {
    if (!pendingTurnKey || pendingGroups.length === 0) return;
    blocks.push({
      type: "activity",
      key: `activity-${pendingTurnKey}`,
      turnKey: pendingTurnKey,
      isComplete: completedTurns.has(pendingTurnKey),
      groups: pendingGroups,
      summary: summarizeActivityGroups(pendingGroups),
    });
    pendingTurnKey = undefined;
    pendingGroups = [];
  };

  renderGroups.forEach((group, index) => {
    const turnKey = resolveTurnKey(group);
    const canGroup = turnKey !== undefined && isCollapsibleIntermediate(group);

    if (canGroup) {
      if (pendingTurnKey && pendingTurnKey !== turnKey) flushPending();
      pendingTurnKey = turnKey;
      pendingGroups.push(group);
      return;
    }

    if (pendingTurnKey) flushPending();
    blocks.push(normalBlock(group, index));
  });

  flushPending();
  return blocks;
}
