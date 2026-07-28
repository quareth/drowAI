/**
 * Subagent activity timeline rendered with the standard chat event components.
 *
 * Responsibilities:
 * - group already-normalized subagent chat messages
 * - mount the existing reasoning/tool/observation/message renderers
 * - keep inner subagent activity out of the main chat transcript
 */
import { useMemo } from "react";

import { ActivityChain } from "@/components/chat/ActivityChain";
import { MessageGroupRenderer } from "@/components/chat/MessageGroup";
import type { ChatMessage } from "@/components/chat/types";
import { groupMessages } from "@/hooks/useMessageGrouping";

interface AgentActivityTimelineProps {
  taskId: number | null | undefined;
  messages: ChatMessage[];
  agentDisplayName?: string;
}

function groupIsStreaming(group: ReturnType<typeof groupMessages>[number]): boolean {
  return group.messages.some((message) => {
    const metadata = message.metadata ?? {};
    return Boolean(
      message.isStreaming ||
        metadata.streaming ||
        metadata.is_streaming ||
        metadata.in_progress,
    );
  });
}

export function AgentActivityTimeline({
  taskId,
  messages,
  agentDisplayName = "subagent",
}: AgentActivityTimelineProps) {
  const groups = useMemo(() => groupMessages(messages), [messages]);
  const activeIndexes = useMemo(
    () =>
      new Set(
        groups
          .map((group, index) => (groupIsStreaming(group) ? index : -1))
          .filter((index) => index >= 0),
      ),
    [groups],
  );

  if (messages.length === 0 || groups.length === 0) {
    return (
      <div
        className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-4 text-sm text-slate-400"
        data-testid="agent-activity-timeline"
      >
        No detailed activity has been received for this {agentDisplayName} run yet.
      </div>
    );
  }

  return (
    <ActivityChain activeIndexes={activeIndexes} testId="agent-activity-timeline">
      {groups.map((group) => (
        <div key={group.key} className="flex">
          <MessageGroupRenderer group={group} taskId={taskId} />
        </div>
      ))}
    </ActivityChain>
  );
}

export default AgentActivityTimeline;
