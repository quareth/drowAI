/**
 * Shared live/completed activity presentation for one parent turn.
 *
 * Live events remain visible in the connected chain. Once the turn completes,
 * the same groups collapse behind the compact summary row.
 */

import { Fragment, type ReactNode } from "react";
import { ChevronDown, ChevronRight, ListChecks } from "lucide-react";

import { useCardToggleState } from "@/hooks/useCardToggleState";
import type { MessageGroup } from "@/hooks/useMessageGrouping";
import { ActivityChain } from "./ActivityChain";
import { MessageGroupRenderer } from "./MessageGroup";
import type { TurnActivityBlock, TurnActivitySummary } from "./turnActivityBlocks";

interface TurnActivityCardProps {
  block: TurnActivityBlock;
  taskId?: number | null;
  onGroupExpand?: (messageId: string) => void;
  onGroupRetry?: (messageId: string) => void;
  renderGroup?: (group: MessageGroup) => ReactNode | undefined;
}

function formatCount(count: number, singular: string, plural: string): string | undefined {
  if (count <= 0) return undefined;
  return `${count} ${count === 1 ? singular : plural}`;
}

function formatSummary(summary: TurnActivitySummary): string {
  const parts = [
    formatCount(summary.toolCount, "tool", "tools"),
    formatCount(summary.thoughtCount, "thought", "thoughts"),
    formatCount(summary.agentCount, "agent", "agents"),
    formatCount(summary.observationCount, "observation", "observations"),
  ].filter((part): part is string => Boolean(part));

  return parts.length > 0 ? parts.join(", ") : "Activity completed";
}

function firstMessageId(group: MessageGroup): string | undefined {
  return group.messages[0]?.id;
}

function readToolCallId(message: MessageGroup["messages"][number]): string | undefined {
  const value = message.metadata?.tool_call_id;
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
}

function splitToolGroupForTranscript(group: MessageGroup): MessageGroup[] {
  if (group.primaryType !== "tool") return [group];

  const buckets = new Map<string, MessageGroup["messages"]>();
  for (const message of group.messages) {
    const toolCallId = readToolCallId(message);
    if (!toolCallId) return [group];
    const bucket = buckets.get(toolCallId) ?? [];
    bucket.push(message);
    buckets.set(toolCallId, bucket);
  }

  if (buckets.size <= 1) return [group];

  return Array.from(buckets.entries()).map(([toolCallId, messages]) => ({
    key: `${group.key}-tool-${toolCallId}`,
    ind: group.ind,
    messages,
    primaryType: "tool",
  }));
}

function expandActivityGroups(groups: MessageGroup[]): MessageGroup[] {
  return groups.flatMap(splitToolGroupForTranscript);
}

function groupIsStreaming(group: MessageGroup): boolean {
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

function renderDetailGroups(
  groups: MessageGroup[],
  {
    taskId,
    onGroupExpand,
    onGroupRetry,
    renderGroup,
  }: Pick<
    TurnActivityCardProps,
    "taskId" | "onGroupExpand" | "onGroupRetry" | "renderGroup"
  >,
): ReactNode[] {
  return groups.map((group) => {
    const messageId = firstMessageId(group);
    const renderedGroup = renderGroup?.(group);
    if (renderedGroup !== undefined) {
      return <Fragment key={group.key}>{renderedGroup}</Fragment>;
    }
    return (
      <MessageGroupRenderer
        key={group.key}
        group={group}
        taskId={taskId}
        onToggleExpand={
          messageId && onGroupExpand ? () => onGroupExpand(messageId) : undefined
        }
        onRetry={messageId && onGroupRetry ? () => onGroupRetry(messageId) : undefined}
      />
    );
  });
}

export function TurnActivityCard({
  block,
  taskId,
  onGroupExpand,
  onGroupRetry,
  renderGroup,
}: TurnActivityCardProps) {
  const stateKey = `turn-activity-${block.turnKey}`;
  const [isOpen, setIsOpen] = useCardToggleState(stateKey, false);
  const label = formatSummary(block.summary);
  const DetailsIcon = isOpen ? ChevronDown : ChevronRight;
  const detailGroups = expandActivityGroups(block.groups);
  const activeIndexes = new Set(
    detailGroups
      .map((group, index) => (groupIsStreaming(group) ? index : -1))
      .filter((index) => index >= 0),
  );
  const detailContent = renderDetailGroups(detailGroups, {
    taskId,
    onGroupExpand,
    onGroupRetry,
    renderGroup,
  });

  if (!block.isComplete) {
    return (
      <div
        className="mb-1 mr-auto block w-full min-w-0 max-w-[calc(100%-2rem)]"
        data-testid={`turn-activity-card-${block.turnKey}`}
      >
        <ActivityChain
          activeIndexes={activeIndexes}
          testId={`turn-activity-details-${block.turnKey}`}
        >
          {detailContent}
        </ActivityChain>
      </div>
    );
  }

  return (
    <div
      className="mb-1 mr-auto block w-full min-w-0 max-w-[calc(100%-2rem)]"
      data-testid={`turn-activity-card-${block.turnKey}`}
    >
      <button
        type="button"
        aria-expanded={isOpen}
        onClick={() => setIsOpen((current) => !current)}
        className="inline-flex max-w-full items-center gap-2 rounded-lg border border-transparent bg-slate-950/40 px-3 py-1.5 text-left text-xs text-slate-400 transition-colors hover:bg-slate-900/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
      >
        <DetailsIcon className="h-3 w-3 shrink-0 text-slate-500" aria-hidden="true" />
        <ListChecks className="h-3 w-3 shrink-0 text-slate-500" aria-hidden="true" />
        <span className="min-w-0 truncate font-medium">{label}</span>
      </button>

      {isOpen && (
        <ActivityChain
          activeIndexes={activeIndexes}
          className="mt-1"
          testId={`turn-activity-details-${block.turnKey}`}
        >
          {detailContent}
        </ActivityChain>
      )}
    </div>
  );
}

export default TurnActivityCard;
