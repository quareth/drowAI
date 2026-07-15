import type { MessageGroup } from "@/hooks/useMessageGrouping";
import { ThinkingCard } from "./ThinkingCard";
import { MessageBubble, type MessageBubbleRetryState } from "./MessageBubble";
import { ObservingCard } from "./ObservingCard";
import { ToolBatchCard } from "./ToolBatchCard";
import type { ChatMessage } from "./types";

interface MessageGroupProps {
  group: MessageGroup;
  taskId?: number | null;
  onToggleExpand?: () => void;
  onRetry?: () => void;
  /**
   * Resolve the in-flight retry lifecycle for a given assistant message.
   * Wired by ``MessageList`` from the task-scoped retry state store so
   * ``MessageBubble`` stays presentational. Returning ``null`` (or
   * omitting the prop) means no retry has been observed for the
   * message and the retry CTA falls back to its server-derived
   * ``retryable`` flag.
   */
  resolveRetryState?: (message: ChatMessage) => MessageBubbleRetryState | null;
}

interface PhaseState {
  hasStart: boolean;
  hasSectionEnd: boolean;
  isInProgress: boolean;
}

function resolveTimestampMs(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return undefined;
}

function getMessageTimestampMs(message: MessageGroup["messages"][number]): number | undefined {
  const direct = resolveTimestampMs(message.timestamp);
  if (direct !== undefined) {
    return direct;
  }
  return resolveTimestampMs((message.metadata as Record<string, unknown> | undefined)?.timestamp);
}

function getStepType(message: MessageGroup["messages"][number]): string | undefined {
  const stepType = message.metadata?.step_type ?? message.metadata?.stepType;
  return typeof stepType === "string" ? stepType : undefined;
}

function getPhaseState(
  messages: MessageGroup["messages"],
  stepTypes: { start: string; sectionEnd: string },
): PhaseState {
  let hasStart = false;
  let hasSectionEnd = false;
  let isSectionOpen = false;

  for (const msg of messages) {
    const stepType = getStepType(msg);
    if (stepType === stepTypes.start) {
      hasStart = true;
      isSectionOpen = true;
    } else if (stepType === stepTypes.sectionEnd) {
      hasSectionEnd = true;
      isSectionOpen = false;
    }
  }

  return {
    hasStart,
    hasSectionEnd,
    isInProgress: hasStart ? isSectionOpen : !hasSectionEnd,
  };
}

/**
 * Renders a group of related messages as a single component.
 * Groups are determined by the `ind` field from the backend.
 */
export function MessageGroupRenderer({ group, taskId, onToggleExpand, onRetry, resolveRetryState }: MessageGroupProps) {
  const { messages, primaryType } = group;
  
  // Reasoning group: accumulate all reasoning_delta content
  if (primaryType === "reasoning") {
    const sections: string[] = [];
    let currentSection = "";
    const { hasStart, isInProgress } = getPhaseState(messages, {
      start: "reasoning_start",
      sectionEnd: "reasoning_section_end",
    });
    let startedAtMs: number | undefined;
    let endedAtMs: number | undefined;
    
    for (const msg of messages) {
      const stepType = getStepType(msg);
      if (stepType === "reasoning_delta") {
        currentSection += msg.content || "";
      } else if (stepType === "reasoning_start" && startedAtMs === undefined) {
        startedAtMs = getMessageTimestampMs(msg);
      } else if (stepType === "reasoning_section_end") {
        if (currentSection.trim().length > 0) {
          sections.push(currentSection);
          currentSection = "";
        }
        endedAtMs = getMessageTimestampMs(msg);
      }
    }
    if (currentSection.trim().length > 0) {
      sections.push(currentSection);
    }
    const hasText = sections.length > 0;
    if ((!hasText && !hasStart) || (!isInProgress && !hasText)) return null;
    const durationMs =
      !isInProgress &&
      typeof startedAtMs === "number" &&
      typeof endedAtMs === "number" &&
      endedAtMs > startedAtMs
        ? endedAtMs - startedAtMs
        : undefined;

    return (
      <ThinkingCard
        steps={sections}
        defaultOpen={false}
        isInProgress={isInProgress}
        durationMs={durationMs}
        stateKey={group.key}
        testId={`reasoning-step-${group.key ?? group.ind}`}
      />
    );
  }

  if (primaryType === "observation") {
    let accumulatedText = "";
    const { hasStart, hasSectionEnd, isInProgress } = getPhaseState(messages, {
      start: "observation_start",
      sectionEnd: "observation_section_end",
    });

    for (const msg of messages) {
      if (getStepType(msg) === "observation_delta") {
        accumulatedText += msg.content || "";
      }
    }

    const hasText = accumulatedText.trim().length > 0;
    if ((!hasText && !hasStart) || (hasSectionEnd && !hasText)) return null;

    return (
      <ObservingCard
        observation={accumulatedText}
        defaultOpen={false}
        isInProgress={isInProgress}
        hasContent={hasText}
        stateKey={group.key}
        testId={`observation-card-${group.key ?? group.ind}`}
      />
    );
  }
  
  // Tool group: prefer the batch card so multi-call batches render with one
  // header and per-row drill-down. Single-call batches keep the legacy
  // ExecutingToolCard appearance via ToolBatchCard's single-row branch.
  if (primaryType === "tool") {
    // Determine if this group is batch-keyed (Task 7.4 grouped on
    // tool_batch_id when present) — we still resolve a taskId to forward.
    let resolvedTaskId: number | string | undefined =
      typeof taskId === "number" ? taskId : undefined;
    if (resolvedTaskId === undefined) {
      for (const msg of messages) {
        const metadataRecord = (msg.metadata || {}) as Record<string, unknown>;
        const candidate = metadataRecord.task_id;
        if (typeof candidate === "number" || typeof candidate === "string") {
          resolvedTaskId = candidate;
          break;
        }
      }
    }
    return (
      <ToolBatchCard
        messages={messages}
        groupKey={group.key}
        taskId={resolvedTaskId}
      />
    );
  }
  
  // Message group: accumulate all message_delta content or use first message
  if (primaryType === "message") {
    // For message groups, we need to accumulate deltas or use the final message
    let accumulatedContent = "";
    let isStreaming = false;
    let finalMessage = messages[messages.length - 1]; // Use last message as base
    
    for (const msg of messages) {
      const stepType = getStepType(msg);
      if (stepType === "message_delta" || stepType === "assistant_delta") {
        accumulatedContent += msg.content || "";
        if (msg.isStreaming) {
          isStreaming = true;
        }
      } else if (stepType === "assistant_message" || stepType === "assistant_final" || !stepType) {
        // Final message or regular message
        finalMessage = msg;
        if (!accumulatedContent) {
          accumulatedContent = msg.content;
        }
      }
    }

    const isTerminalNotice =
      finalMessage.metadata?.status === "error" ||
      finalMessage.metadata?.status === "declined";
    const resolvedContent = isTerminalNotice
      ? finalMessage.content || accumulatedContent
      : accumulatedContent || finalMessage.content;
    if (!isTerminalNotice && resolvedContent.trim().length === 0) {
      return null;
    }
    
    // Create a merged message for rendering
    const mergedMessage = {
      ...finalMessage,
      content: resolvedContent,
      isStreaming: isTerminalNotice ? false : isStreaming,
    };
    
    return (
      <MessageBubble
        message={mergedMessage}
        onExpand={onToggleExpand ? () => onToggleExpand() : undefined}
        onRetry={onRetry ? () => onRetry() : undefined}
        retryState={resolveRetryState ? resolveRetryState(mergedMessage) : null}
      />
    );
  }

  // Fallback: render first message as-is (for legacy messages without proper grouping)
  const firstMessage = messages[0];
  if (!firstMessage) return null;

  return (
    <MessageBubble
      message={firstMessage}
      onExpand={onToggleExpand ? () => onToggleExpand() : undefined}
      onRetry={onRetry ? () => onRetry() : undefined}
      retryState={resolveRetryState ? resolveRetryState(firstMessage) : null}
    />
  );
}
