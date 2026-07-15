import type { ChatMessage } from "@/components/chat/types";
import { getConversationId } from "@/utils/chatMeta";
import { featureFlags } from "@/config/feature-flags";

export function filterChatMessages(messages: ChatMessage[]): ChatMessage[] {
  if (!featureFlags.enableBasicChat) {
    return messages;
  }
  return messages.filter((m) => {
    // Keep tool and reasoning events - they'll render as their own bubbles
    const metadata = m.metadata || {};
    const stepType = metadata.stepType || metadata.step_type || m.type;

    if (metadata.internal_only === true) {
      return false;
    }

    if (
      stepType === "assistant_final" ||
      stepType === "status" ||
      stepType === "message_start" ||
      stepType === "message_section_end" ||
      stepType === "section_end"
    ) {
      return false;
    }
    
    // Filter out only tool_delta (progress updates that just replace tool_start)
    if (stepType === "tool_delta") {
      return false;
    }
    
    // Check if this is a special event type first (tool/reasoning events)
    const isSpecialEvent = 
      stepType === "reasoning_start" || stepType === "reasoning_delta" || stepType === "reasoning_section_end" ||
      stepType === "tool_start" || stepType === "tool_end" || stepType === "tool_progress" ||
      stepType === "observation_start" || stepType === "observation_delta" || stepType === "observation_section_end";
    
    if (isSpecialEvent) {
      return true; // Always keep tool/reasoning events
    }
    
    // For optimistic user messages, allow pending entries even before convo id is known.
    if (m.type === "user" && metadata.status === "pending") {
      return true;
    }

    // For regular messages, check conversation ID
    const cid = getConversationId(m.metadata);
    const hasConversation = typeof cid === "string" && cid.length > 0;
    if (!hasConversation && !m.isStreaming) return false;
    
    return m.type === "user" || m.type === "agent" || m.type === "system";
  });
}


