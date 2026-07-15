import type { StreamEvent, StreamEventMetadata } from "../types/reasoning-events";
import type { StreamPacket } from "@/types/packets";
import { isStreamPacket } from "@/types/packets";

export type StepMetadata = StreamEventMetadata | undefined;

export interface Step extends StreamEvent {
  metadata?: StepMetadata;
  __internalKey?: string;
  isStreaming?: boolean;
}

/** Optional metadata preserved by SSE endpoint for frontend grouping (ind, step_type). */
export interface OpenAIChunkMetadata {
  ind?: number;
  step_type?: string;
  conversation_id?: string;
  conversationId?: string;
  streaming?: boolean;
}

export interface OpenAIChunk {
  id?: string;
  object: string;
  taskId?: number;
  sequence?: number;
  /** When present, preserves ind/step_type so observation and message render separately. */
  metadata?: OpenAIChunkMetadata;
  choices?: Array<{ delta?: { content?: string } }>;
}

/**
 * For observation/reasoning steps with sub_turn_index, we must not use the
 * meta-id key so that deriveStepKey falls through to the turn-ind-sub path.
 * Otherwise all DR iterations merge into one store item (Issue 16 Fix B regression).
 */
function deriveKeyFromMetadata(metadata: StepMetadata, stepType: string): string | null {
  if (!metadata) return null;
  const meta = metadata as Record<string, unknown>;
  if (isReasoningLifecycleStep(stepType)) {
    const reasoningSectionId = meta.reasoning_section_id;
    if (typeof reasoningSectionId !== "string" || reasoningSectionId.length === 0) {
      throw new Error(`Reasoning event missing reasoning_section_id for ${stepType}`);
    }
    return `reasoning-${reasoningSectionId}-${stepType}`;
  }

  const isObservationOrReasoning =
    stepType.startsWith("observation") || stepType.startsWith("reasoning");
  const subTurnIndex = resolveCanonicalSubTurnIndex(meta, stepType);
  if (
    isObservationOrReasoning &&
    typeof subTurnIndex === "number"
  ) {
    return null;
  }

  const clientMessageId = meta.client_message_id;
  if (typeof clientMessageId === "string" && clientMessageId.length > 0) {
    return `client-${clientMessageId}`;
  }

  const toolCallId = meta.tool_call_id;
  if (typeof toolCallId === "string" && toolCallId.length > 0) {
    return `tool-${toolCallId}-${stepType}`;
  }

  const candidates = ["id", "uuid", "message_id", "request_id"];
  for (const key of candidates) {
    const value = meta[key];
    if (typeof value === "string" && value.length > 0) {
      return `meta-${key}-${value}-${stepType}`;
    }
  }
  return null;
}

function normalizeStepTypeForKey(stepType: string): string {
  if (!stepType) return stepType;
  switch (stepType) {
    case "assistant_delta":
    case "assistant_stream":
      return "message_delta";
    default:
      return stepType;
  }
}

function isReasoningLifecycleStep(stepType: string): boolean {
  return (
    stepType === "reasoning_start" ||
    stepType === "reasoning_delta" ||
    stepType === "reasoning_section_end"
  );
}

function resolveCanonicalSubTurnIndex(meta: Record<string, unknown>, stepType: string): number | undefined {
  const explicitSubTurnIndex = meta.sub_turn_index;
  if (typeof explicitSubTurnIndex === "number") {
    return explicitSubTurnIndex;
  }
  // Canonicalize first observation section identity across live/replay:
  // replay may normalize missing first section to 0 while live may omit it.
  if (stepType.startsWith("observation")) {
    return 0;
  }
  return undefined;
}

export function deriveStepKey(step: Step): string {
  const meta = (step.metadata as Record<string, unknown>) || {};
  const rawStepType = typeof meta.step_type === "string" ? (meta.step_type as string) : step.type;
  const stepType = normalizeStepTypeForKey(rawStepType);
  const canonicalSubTurnIndex = resolveCanonicalSubTurnIndex(meta, stepType);
  const metadataKey = deriveKeyFromMetadata(step.metadata, stepType);
  if (metadataKey) {
    return metadataKey;
  }

  const conversationId = (meta.conversation_id as string) ?? (meta.conversationId as string) ?? "";
  const turnId = (meta.id as string) ?? "";
  const ind = meta.ind as number | undefined;
  const turnSequence = (meta.turn_sequence as number | undefined) ?? step.sequence;
  const subTurnIndex = canonicalSubTurnIndex;
  if (turnId && typeof ind === "number") {
    const subSuffix = typeof subTurnIndex === "number" ? `-sub-${subTurnIndex}` : "";
    return `turn-${turnId}-ind-${ind}${subSuffix}-${stepType}`;
  }

  if (turnId) {
    return `turn-${turnId}-${stepType}`;
  }

  if (conversationId && typeof ind === "number" && typeof turnSequence === "number") {
    const subSuffix = typeof subTurnIndex === "number" ? `-sub-${subTurnIndex}` : "";
    return `conv-${conversationId}-seq-${turnSequence}-ind-${ind}${subSuffix}-${stepType}`;
  }

  // Use `ind` for grouping if available (groups related events like reasoning, tool, message)
  const baseSequence = typeof turnSequence === "number" ? turnSequence : step.sequence;
  if (typeof ind === "number" && typeof baseSequence === "number") {
    const subSuffix = typeof subTurnIndex === "number" ? `-sub-${subTurnIndex}` : "";
    return `seq-${baseSequence}-ind-${ind}${subSuffix}-${stepType}`;
  }
  
  // Fallback: include event type in key to prevent different event types from overwriting each other
  if (typeof step.sequence === "number") {
    return `seq-${step.sequence}-${step.type}`;
  }
  const timestampKey = (step.timestamp || ((step.metadata as any)?.timestamp ?? "")).toString();
  const contentSnippet = (step.content || "").slice(0, 24);
  return `fallback-${step.type}-${timestampKey}-${contentSnippet}`;
}

function packetToStep(packet: StreamPacket): Step {
  const event = packet.obj;
  const metadata = event.metadata ? { ...event.metadata } : {};
  if (typeof packet.sequence === "number" && typeof (metadata as any).sequence_authority !== "string") {
    metadata.sequence = packet.sequence;
    (metadata as any).sequence_authority = "live_stream";
  }
  if (typeof metadata.ind !== "number" && typeof packet.placement?.tab_index === "number") {
    metadata.ind = packet.placement.tab_index;
  }
  if (
    typeof metadata.turn_sequence !== "number" &&
    typeof packet.placement?.turn_index === "number"
  ) {
    metadata.turn_sequence = packet.placement.turn_index;
  }
  if (
    typeof (metadata as any).sub_turn_index !== "number" &&
    typeof packet.placement?.sub_turn_index === "number"
  ) {
    (metadata as any).sub_turn_index = packet.placement.sub_turn_index;
  }
  return {
    ...event,
    metadata,
    sequence: typeof packet.sequence === "number" ? packet.sequence : event.sequence,
    timestamp: event.timestamp,
  } as Step;
}

function normalizeStepEvent(step: Step): Step {
  const metadata = step.metadata ? { ...step.metadata } : undefined;
  const turnSequence = typeof metadata?.turn_sequence === "number" ? metadata.turn_sequence : undefined;
  const metadataSequence = typeof metadata?.sequence === "number" ? metadata.sequence : undefined;
  const baseSequence = typeof step.sequence === "number" ? step.sequence : metadataSequence;
  const sequence = turnSequence ?? baseSequence;
  const rawTimestamp = step.timestamp ?? metadata?.timestamp;
  const timestamp =
    typeof rawTimestamp === "string"
      ? rawTimestamp
      : typeof rawTimestamp === "number" && Number.isFinite(rawTimestamp)
        ? new Date(rawTimestamp * 1000).toISOString()
        : undefined;
  const isStreaming = Boolean(
    (metadata && (metadata.streaming || metadata.is_streaming || metadata.in_progress)) || step.isStreaming,
  );

  const normalized: Step = {
    ...step,
    sequence,
    metadata,
    timestamp,
    isStreaming,
  };
  normalized.__internalKey = step.__internalKey ?? deriveStepKey(normalized);
  return normalized;
}

export function normalizeStep(step: Step | StreamPacket): Step {
  if (isStreamPacket(step)) {
    return normalizeStepEvent(packetToStep(step));
  }
  return normalizeStepEvent(step);
}

function resolveStepType(step: Step): string {
  const metadata = (step.metadata as Record<string, unknown>) || {};
  return typeof metadata.step_type === "string" ? metadata.step_type : step.type;
}

function resolveSequenceAuthority(step: Step): string | undefined {
  const metadata = (step.metadata as Record<string, unknown>) || {};
  const value = metadata.sequence_authority;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function isMessagePhaseStep(step: Step): boolean {
  const stepType = resolveStepType(step);
  return (
    stepType === "message_start" ||
    stepType === "message_delta" ||
    stepType === "assistant_delta" ||
    stepType === "assistant_message" ||
    stepType === "assistant_final"
  );
}

function canUseWithinTurnSequence(step: Step): boolean {
  const metadata = (step.metadata as Record<string, unknown>) || {};
  const sequence = metadata.sequence;
  if (typeof sequence !== "number") {
    return false;
  }

  const authority = resolveSequenceAuthority(step);
  if (authority === "canonical_detail") {
    return true;
  }
  if (authority === "synthetic_message" || authority === "legacy_reasoning_blob") {
    return false;
  }
  if (isMessagePhaseStep(step)) {
    return false;
  }
  return true;
}

function normalizeStatus(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function isCancelStatus(value: unknown): boolean {
  const normalized = normalizeStatus(value);
  return (
    normalized === "cancelled" ||
    normalized === "canceled" ||
    normalized === "cancel_requested" ||
    normalized === "stopped"
  );
}

function isSuccessfulTerminalStatus(value: unknown): boolean {
  const normalized = normalizeStatus(value);
  return normalized === "success" || normalized === "ok" || normalized === "completed";
}

function shouldPreserveCancelProjection(
  existingMetadata: StepMetadata,
  incomingMetadata: StepMetadata,
): boolean {
  const existing = (existingMetadata ?? {}) as Record<string, unknown>;
  const incoming = (incomingMetadata ?? {}) as Record<string, unknown>;
  const incomingStepType = typeof incoming.step_type === "string" ? incoming.step_type : "";
  if (!isCancelStatus(existing.status)) {
    return false;
  }
  if (existing.cancellation_source !== "chat_stop") {
    return false;
  }
  if (incomingStepType !== "tool_end" && incomingStepType !== "tool_batch_end") {
    return false;
  }
  return isSuccessfulTerminalStatus(incoming.status);
}

export const STEP_COMPARATOR = (a: Step, b: Step): number => {
  // 1) Cross-turn ordering by turn_sequence / top-level sequence.
  const turnSeqA = (a.metadata as any)?.turn_sequence;
  const turnSeqB = (b.metadata as any)?.turn_sequence;
  const seqA =
    typeof turnSeqA === "number"
      ? turnSeqA
      : typeof a.sequence === "number"
        ? a.sequence
        : Number.MAX_SAFE_INTEGER;
  const seqB =
    typeof turnSeqB === "number"
      ? turnSeqB
      : typeof b.sequence === "number"
        ? b.sequence
        : Number.MAX_SAFE_INTEGER;
  if (seqA !== seqB) {
    return seqA - seqB;
  }

  // 2) Within-turn ordering by canonical metadata.sequence when both steps
  //    carry it. This is the primary within-turn authority for persisted
  //    replay payloads, preventing re-sorting away from persisted order.
  const metaSeqA = (a.metadata as any)?.sequence;
  const metaSeqB = (b.metadata as any)?.sequence;
  const canUseMetaSeqA = canUseWithinTurnSequence(a);
  const canUseMetaSeqB = canUseWithinTurnSequence(b);
  if (
    canUseMetaSeqA &&
    canUseMetaSeqB &&
    typeof metaSeqA === "number" &&
    typeof metaSeqB === "number" &&
    metaSeqA !== metaSeqB
  ) {
    return metaSeqA - metaSeqB;
  }

  // 3) Phase ordering via `ind` as fallback when canonical sequence is
  //    absent on one or both steps (e.g. live events before persistence):
  //    reasoning (0) -> tool/observation (1) -> final answer/message (2).
  const indA = (a.metadata as any)?.ind;
  const indB = (b.metadata as any)?.ind;
  if (typeof indA === "number" && typeof indB === "number" && indA !== indB) {
    return indA - indB;
  }

  if (canUseMetaSeqA || canUseMetaSeqB) {
    if (!canUseMetaSeqA) {
      return 1;
    }
    if (!canUseMetaSeqB) {
      return -1;
    }
  }

  // 4) Fallback: sub_turn_index separates DR iterations when
  //    metadata.sequence is missing on live events.
  const subA = (a.metadata as any)?.sub_turn_index;
  const subB = (b.metadata as any)?.sub_turn_index;
  if (typeof subA === "number" && typeof subB === "number" && subA !== subB) {
    return subA - subB;
  }

  const tsA = a.timestamp ?? "";
  const tsB = b.timestamp ?? "";
  if (tsA !== tsB) {
    return tsA.localeCompare(tsB);
  }
  const keyA = a.__internalKey ?? "";
  const keyB = b.__internalKey ?? "";
  return keyA.localeCompare(keyB);
};

export function mergeStepContent(existing: Step, incoming: Step): Step {
  const mergedMetadata = {
    ...(existing.metadata ?? {}),
    ...(incoming.metadata ?? {}),
  } as Record<string, unknown>;
  if (shouldPreserveCancelProjection(existing.metadata, incoming.metadata)) {
    const existingMetadata = (existing.metadata ?? {}) as Record<string, unknown>;
    mergedMetadata.status = existingMetadata.status;
    mergedMetadata.cancellation_source = existingMetadata.cancellation_source;
    mergedMetadata.process_state = existingMetadata.process_state;
    if (Array.isArray(existingMetadata.results)) {
      mergedMetadata.results = existingMetadata.results;
    }
  }
  const existingContent = typeof existing.content === "string" ? existing.content : "";
  const incomingContent = typeof incoming.content === "string" ? incoming.content : "";
  const existingStreaming = Boolean(
    existing.isStreaming ||
      (existing.metadata as any)?.streaming ||
      (existing.metadata as any)?.is_streaming ||
      (existing.metadata as any)?.in_progress,
  );
  const incomingStreaming = Boolean(
    incoming.isStreaming ||
      (incoming.metadata as any)?.streaming ||
      (incoming.metadata as any)?.is_streaming ||
      (incoming.metadata as any)?.in_progress,
  );
  const isStreamingUpdate = Boolean(
    incomingStreaming || existingStreaming || mergedMetadata.streaming || mergedMetadata.is_streaming || mergedMetadata.in_progress,
  );
  const stepType = typeof mergedMetadata.step_type === "string" ? (mergedMetadata.step_type as string) : "";
  const isSectionEnd = stepType.endsWith("_section_end");

  const merged: Step = {
    ...existing,
    ...incoming,
    metadata: mergedMetadata,
  };

  if (isStreamingUpdate) {
    if (!incomingContent) {
      merged.content = existingContent;
    } else if (!existingContent) {
      merged.content = incomingContent;
    } else if (existingStreaming && !incomingStreaming) {
      // Snapshot beats streaming chunks to avoid duplicate append on completion.
      merged.content = incomingContent;
    } else if (!existingStreaming && incomingStreaming) {
      // Ignore late streaming chunks once a snapshot is present.
      if (incomingContent.startsWith(existingContent)) {
        merged.content = incomingContent;
      } else if (existingContent.startsWith(incomingContent)) {
        merged.content = existingContent;
      } else {
        merged.content = existingContent;
      }
    } else if (incomingContent.startsWith(existingContent)) {
      merged.content = incomingContent;
    } else if (existingContent.startsWith(incomingContent)) {
      merged.content = existingContent;
    } else {
      merged.content = `${existingContent}${incomingContent}`;
    }
  } else if (incoming.content !== undefined && !isSectionEnd) {
    merged.content = incoming.content;
  } else if (!merged.content && existing.content) {
    merged.content = existing.content;
  }

  merged.isStreaming = Boolean(mergedMetadata.streaming || mergedMetadata.is_streaming || mergedMetadata.in_progress);
  merged.__internalKey = existing.__internalKey || incoming.__internalKey;
  return merged;
}
