/**
 * Lossless projection from canonical Step to ChatMessage.
 *
 * Steps in the chat-stream-store are already normalized at ingestion.
 * This function adapts the shape for the rendering layer WITHOUT
 * re-parsing or filtering metadata. All Step metadata is passed through
 * directly so downstream consumers (filters, grouping, card renderers)
 * see every field the backend produced.
 */

import type { Step } from "@/utils/reasoning-normalizer";
import type { ChatMessage, ChatMessageType } from "@/components/chat/types";

function resolveMessageType(step: Step): ChatMessageType {
  const meta = step.metadata as Record<string, unknown> | undefined;

  const role = meta?.role as string | undefined;
  if (role === "user") return "user";
  if (role === "system") return "system";

  const stepType = (meta?.step_type ?? meta?.stepType ?? step.type) as string | undefined;
  if (!stepType) return "agent";

  const lower = stepType.toLowerCase();
  if (lower.includes("user")) return "user";
  if (lower.includes("system")) return "system";
  if (lower.includes("tool") || lower.includes("execute") || lower.includes("action")) return "executing";
  if (lower.includes("reason") || lower.includes("think") || lower.includes("analysis")) return "thinking";
  return "agent";
}

function resolveId(step: Step): string {
  if (step.__internalKey) return step.__internalKey;

  const meta = step.metadata as Record<string, unknown> | undefined;
  if (typeof meta?.client_message_id === "string") return meta.client_message_id;
  if (typeof meta?.id === "string") return meta.id;

  const seq = meta?.turn_sequence ?? meta?.sequence ?? step.sequence;
  if (typeof seq === "number") return `seq-${seq}`;
  if (step.timestamp) return `ts-${step.timestamp}`;

  return `step-${Math.random().toString(36).slice(2)}`;
}

export function stepToChatMessage(step: Step): ChatMessage {
  return {
    id: resolveId(step),
    type: resolveMessageType(step),
    content: typeof step.content === "string" ? step.content : "",
    timestamp: step.timestamp ?? new Date().toISOString(),
    isStreaming: Boolean(step.isStreaming),
    metadata: step.metadata as ChatMessage["metadata"],
  };
}
