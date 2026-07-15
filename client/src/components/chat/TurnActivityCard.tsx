/**
 * Compact completed-turn activity row for collapsed intermediate events.
 *
 * The expanded body delegates back to MessageGroupRenderer so reasoning,
 * completed tools, observations, and raw-output expansion keep their existing UI.
 */

import { ChevronDown, ChevronRight, ListChecks } from "lucide-react";

import { useCardToggleState } from "@/hooks/useCardToggleState";
import type { MessageGroup } from "@/hooks/useMessageGrouping";
import { MessageGroupRenderer } from "./MessageGroup";
import type { TurnActivityBlock, TurnActivitySummary } from "./turnActivityBlocks";

interface TurnActivityCardProps {
  block: TurnActivityBlock;
  taskId?: number | null;
  onGroupExpand?: (messageId: string) => void;
  onGroupRetry?: (messageId: string) => void;
}

function formatCount(count: number, singular: string, plural: string): string | undefined {
  if (count <= 0) return undefined;
  return `${count} ${count === 1 ? singular : plural}`;
}

function formatSummary(summary: TurnActivitySummary): string {
  const parts = [
    formatCount(summary.toolCount, "tool", "tools"),
    formatCount(summary.thoughtCount, "thought", "thoughts"),
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

export function TurnActivityCard({
  block,
  taskId,
  onGroupExpand,
  onGroupRetry,
}: TurnActivityCardProps) {
  const stateKey = `turn-activity-${block.turnKey}`;
  const [isOpen, setIsOpen] = useCardToggleState(stateKey, false);
  const label = formatSummary(block.summary);
  const DetailsIcon = isOpen ? ChevronDown : ChevronRight;
  const detailGroups = expandActivityGroups(block.groups);

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
        <div
          className="mt-1 flex min-w-0 flex-col gap-1"
          data-testid={`turn-activity-details-${block.turnKey}`}
        >
          {detailGroups.map((group) => {
            const messageId = firstMessageId(group);
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
          })}
        </div>
      )}
    </div>
  );
}

export default TurnActivityCard;
