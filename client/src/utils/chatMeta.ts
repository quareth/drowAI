// Centralized helpers for chat metadata handling
import type { ChatMessage } from "@/components/chat/types";

export function getConversationId(meta: ChatMessage["metadata"] | undefined): string | null {
  const anyMeta = (meta ?? {}) as any;
  const cid = anyMeta?.conversationId ?? anyMeta?.conversation_id;
  return typeof cid === "string" && cid.length > 0 ? cid : null;
}

export function isStreamingMessage(msg: ChatMessage): boolean {
  return Boolean(msg?.isStreaming);
}


