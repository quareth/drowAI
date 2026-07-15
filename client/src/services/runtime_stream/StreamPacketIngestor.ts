/**
 * StreamPacketIngestor validates and ingests runtime stream envelopes.
 *
 * Responsibility:
 * - validate incoming agent_reasoning envelopes
 * - normalize task/sequence metadata for task-local cursor safety
 * - dispatch packet payloads into chat-stream-store without UI concerns
 */

import type { StreamEvent } from "@/types/packets";
import { isStreamPacket, type StreamPacket } from "@/types/packets";
import { advanceStreamSequence, applyStreamMessage } from "@/state/chat-stream-store";

import type { RuntimeAgentReasoningEnvelope } from "./types";

type IngestCandidate = StreamPacket | StreamEvent;

const NON_TRANSCRIPT_EVENT_TYPES = new Set(["plan_created", "todo_progress"]);

function isStreamEvent(value: unknown): value is StreamEvent {
  if (!value || typeof value !== "object") {
    return false;
  }
  const event = value as StreamEvent;
  return typeof event.type === "string";
}

function coercePositiveInt(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const normalized = Math.floor(value);
  return normalized > 0 ? normalized : null;
}

function coerceSequence(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const normalized = Math.floor(value);
  return normalized >= 0 ? normalized : null;
}

function normalizeEnvelopePayload(
  taskId: number,
  sequence: number | null,
  payload: unknown,
): IngestCandidate | null {
  if (isStreamPacket(payload)) {
    return {
      ...payload,
      task_id: typeof payload.task_id === "number" ? payload.task_id : taskId,
      sequence: typeof payload.sequence === "number" ? payload.sequence : (sequence ?? undefined),
    };
  }
  if (isStreamEvent(payload)) {
    return {
      ...payload,
      task_id: typeof payload.task_id === "number" ? payload.task_id : taskId,
      sequence: typeof payload.sequence === "number" ? payload.sequence : (sequence ?? undefined),
      metadata: {
        ...(payload.metadata ?? {}),
        sequence:
          typeof payload.metadata?.sequence === "number"
            ? payload.metadata.sequence
            : (sequence ?? undefined),
      },
    };
  }
  return null;
}

function resolveEventType(candidate: IngestCandidate): string | null {
  if (isStreamPacket(candidate)) {
    return typeof candidate.obj?.type === "string" ? candidate.obj.type : null;
  }
  return typeof candidate.type === "string" ? candidate.type : null;
}

function isTranscriptEligible(candidate: IngestCandidate): boolean {
  const eventType = resolveEventType(candidate);
  return eventType === null || !NON_TRANSCRIPT_EVENT_TYPES.has(eventType);
}

export class StreamPacketIngestor {
  public ingestEnvelope(envelope: RuntimeAgentReasoningEnvelope): boolean {
    const taskId = coercePositiveInt(envelope.taskId);
    if (taskId === null) {
      return false;
    }

    const sequence = coerceSequence(envelope.sequence);
    if (sequence !== null) {
      advanceStreamSequence(taskId, sequence);
    }

    const normalized = normalizeEnvelopePayload(taskId, sequence, envelope.packet);
    if (!normalized) {
      return false;
    }
    if (!isTranscriptEligible(normalized)) {
      return true;
    }
    applyStreamMessage(taskId, normalized, sequence ?? undefined);
    return true;
  }
}

export default StreamPacketIngestor;
