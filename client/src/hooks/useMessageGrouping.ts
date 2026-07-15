/**
 * Groups normalized chat messages according to the streaming contract.
 *
 * Messages are bucketed by phase (`ind`) and turn id so Think/Tool/Answer cards
 * render consistently regardless of which backend graph produced the events.
 */

import { useMemo } from "react";
import type { ChatMessage } from "@/components/chat/types";

export interface MessageGroup {
  key: string;
  ind: number;
  messages: ChatMessage[];
  primaryType: "user" | "reasoning" | "tool" | "observation" | "message" | "other";
}

function coerceNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return undefined;
}

function resolveSequenceAuthority(message: ChatMessage): string | undefined {
  const value = message.metadata?.sequence_authority;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function isCanonicalDetailReplay(message: ChatMessage): boolean {
  return resolveSequenceAuthority(message) === "canonical_detail";
}

function isReasoningLifecycleStep(stepType: string | undefined): boolean {
  return (
    stepType === "reasoning_start" ||
    stepType === "reasoning_delta" ||
    stepType === "reasoning_section_end"
  );
}

/**
 * Groups messages by their `ind` field (streaming packet grouping).
 * Messages with the same `ind` value are related and should render as a single component.
 * 
 * @param messages - Array of chat messages
 * @returns Array of message groups, ordered by first message appearance
 */
export function groupMessages(messages: ChatMessage[]): MessageGroup[] {
  type GroupBucket = { ind: number; messages: ChatMessage[] };
  const groupMap = new Map<string, GroupBucket>();
  const reasoningSectionState = new Map<string, { sectionIndex: number; sectionClosed: boolean }>();

  for (const message of messages) {
    const phase = typeof message.metadata?.ind === "number" ? message.metadata!.ind! : -1;
    const stepType = (message.metadata?.step_type ?? (message.metadata as any)?.stepType) as
      | string
      | undefined;
    const isMessagePhase =
      stepType === "message_start" ||
      stepType === "message_delta" ||
      stepType === "assistant_delta" ||
      stepType === "assistant_message" ||
      stepType === "assistant_final";
    const isReasoningPhase =
      typeof stepType === "string" &&
      (stepType.startsWith("reasoning") || stepType === "retry_start" || stepType === "retry_attempt");
    const isToolPhase = Boolean(stepType?.startsWith("tool"));
    const toolCallId =
      typeof (message.metadata as any)?.tool_call_id === "string"
        ? (message.metadata as any).tool_call_id
        : undefined;
    // Live batch runs group by tool_batch_id. Canonical replay keeps the batch
    // id as metadata, but renders durable tool calls individually.
    const toolBatchId =
      !isCanonicalDetailReplay(message) &&
      typeof (message.metadata as any)?.tool_batch_id === "string" &&
      (message.metadata as any).tool_batch_id.length > 0
        ? (message.metadata as any).tool_batch_id
        : undefined;

    let baseId: string;
    if (isReasoningLifecycleStep(stepType)) {
      const reasoningSectionId = (message.metadata as any)?.reasoning_section_id;
      if (typeof reasoningSectionId !== "string" || reasoningSectionId.length === 0) {
        throw new Error(`Reasoning message missing reasoning_section_id for ${stepType}`);
      }
      baseId = `reasoning-${reasoningSectionId}`;
    } else if (isMessagePhase) {
      const turnSeqForKey = coerceNumber(message.metadata?.turn_sequence);
      if (turnSeqForKey != null) {
        baseId = `msg-${turnSeqForKey}`;
      } else {
        baseId =
          (message.metadata?.id as string | undefined) ??
          message.id ??
          `msg-${groupMap.size}`;
      }
    } else if (isToolPhase && (toolBatchId || toolCallId)) {
      baseId = toolBatchId ? `tool-batch-${toolBatchId}` : `tool-${toolCallId}`;
    } else {
      const rawBaseId =
        (message.metadata?.id as string | undefined) ??
        message.id ??
        `msg-${groupMap.size}`;
      const subIdx = (message.metadata as any)?.sub_turn_index;
      const subSuffix = typeof subIdx === "number" ? `-sub-${subIdx}` : "";
      baseId = `${rawBaseId}${subSuffix}`;
    }

    const baseGroupKey = `${phase}:${baseId}`;
    let groupKey = baseGroupKey;

    if (isReasoningPhase && !isReasoningLifecycleStep(stepType)) {
      const sectionState =
        reasoningSectionState.get(baseGroupKey) ?? { sectionIndex: 0, sectionClosed: false };

      // When a new reasoning section starts after a completed one in the same
      // turn/key, split it into a distinct frontend group.
      if (stepType === "reasoning_start" && sectionState.sectionClosed) {
        sectionState.sectionIndex += 1;
        sectionState.sectionClosed = false;
      }

      groupKey = `${baseGroupKey}:reasoning-${sectionState.sectionIndex}`;

      if (stepType === "reasoning_section_end") {
        sectionState.sectionClosed = true;
      }

      reasoningSectionState.set(baseGroupKey, sectionState);
    }

    if (!groupMap.has(groupKey)) {
      groupMap.set(groupKey, { ind: phase, messages: [] });
    }
    groupMap.get(groupKey)!.messages.push(message);
  }

  // Convert to array and determine primary type for each group
  const groups: MessageGroup[] = [];

  for (const [groupKey, bucket] of groupMap.entries()) {
    const { ind, messages: groupMessages } = bucket;
    // Determine primary type based on step_type of messages in group
    let primaryType: MessageGroup["primaryType"] = "other";

    for (const msg of groupMessages) {
      if (msg.type === "user") {
        primaryType = "user";
        break;
      }
      const stepType = msg.metadata?.step_type ?? (msg.metadata as any)?.stepType;

      if (
        stepType?.startsWith("reasoning") ||
        stepType === "retry_start" ||
        stepType === "retry_attempt"
      ) {
        primaryType = "reasoning";
        break;
      } else if (stepType?.startsWith("tool")) {
        primaryType = "tool";
        break;
      } else if (stepType?.startsWith("observation")) {
        primaryType = "observation";
        break;
      } else if (
        stepType === "message_start" ||
        stepType === "message_delta" ||
        stepType === "assistant_delta" ||
        stepType === "assistant_message" ||
        stepType === "assistant_final"
      ) {
        primaryType = "message";
        // Don't break - reasoning/tool take precedence
      }
    }
    groups.push({
      key: groupKey,
      ind,
      messages: groupMessages,
      primaryType,
    });
  }

  const getOrderingSequence = (msg: ChatMessage): number => {
    const turnSeq = coerceNumber(msg.metadata?.turn_sequence);
    const seq = coerceNumber(msg.metadata?.sequence);
    return turnSeq ?? seq ?? 0;
  };

  const canUseWithinTurnSequence = (group: MessageGroup): boolean => {
    const first = group.messages[0];
    const sequence = coerceNumber(first.metadata?.sequence);
    if (sequence === undefined) {
      return false;
    }

    const authority = resolveSequenceAuthority(first);
    if (authority === "canonical_detail") {
      return true;
    }
    if (authority === "synthetic_message" || authority === "legacy_reasoning_blob") {
      return false;
    }
    if (group.primaryType === "message") {
      return false;
    }
    return true;
  };

  /**
   * Semantic phase ranking used as a secondary sort key when turn/sequence
   * are equal.
   *
   * Design intent:
   * - Keep reasoning (Thinking card) visually above other phases for a turn.
   * - Let tool + observation phases share the same rank so their relative
   *   order is driven by actual stream order (timestamp), avoiding surprises
   *   where a late tool execution appears "above" earlier observations.
   * - Keep the final answer / generic messages last.
   */
  const phaseRank = (group: MessageGroup): number => {
    switch (group.primaryType) {
      case "user":
        return -1;
      case "reasoning":
        return 0;
      case "tool":
      case "observation":
        return 1;
      case "message":
        return 2;
      default:
        return 99;
    }
  };

  // Sort groups: turn identity first, then canonical within-turn sequence
  // before semantic phase rank so that interleaved reasoning sections
  // maintain their persisted position among tool/observation groups.
  groups.sort((a, b) => {
    const aFirst = a.messages[0];
    const bFirst = b.messages[0];

    // 1) Cross-turn ordering by turn_sequence
    const aSeq = getOrderingSequence(aFirst);
    const bSeq = getOrderingSequence(bFirst);
    if (aSeq !== bSeq) return aSeq - bSeq;

    // 2) Within-turn ordering by canonical phase_sequence when both groups
    //    carry it — this preserves exact interleaving from persistence.
    const aPhaseSequence = coerceNumber(aFirst.metadata?.sequence);
    const bPhaseSequence = coerceNumber(bFirst.metadata?.sequence);
    const aCanUsePhaseSequence = canUseWithinTurnSequence(a);
    const bCanUsePhaseSequence = canUseWithinTurnSequence(b);
    if (aCanUsePhaseSequence && bCanUsePhaseSequence && aPhaseSequence !== undefined && bPhaseSequence !== undefined) {
      if (aPhaseSequence !== bPhaseSequence) return aPhaseSequence - bPhaseSequence;
    }

    // 3) Semantic phase rank as fallback when canonical sequence is absent
    //    on one or both groups (e.g. live events before persistence).
    const aRank = phaseRank(a);
    const bRank = phaseRank(b);
    if (aRank !== bRank) return aRank - bRank;

    // 4) If one group has a phase sequence and the other does not,
    //    prefer the one with a defined sequence.
    if (aCanUsePhaseSequence || bCanUsePhaseSequence) {
      if (!aCanUsePhaseSequence) return 1;
      if (!bCanUsePhaseSequence) return -1;
    }

    // 5) Fallback to timestamp
    return aFirst.timestamp.localeCompare(bFirst.timestamp);
  });

  return groups;
}

export function useMessageGrouping(messages: ChatMessage[]): MessageGroup[] {
  return useMemo(() => groupMessages(messages), [messages]);
}
