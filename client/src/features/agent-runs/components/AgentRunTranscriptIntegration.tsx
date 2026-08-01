/**
 * Agent-run integration boundary for the generic chat transcript.
 *
 * Owns run hydration, transcript projection, card substitution, cancellation,
 * drawer navigation, and the resizable conversation/drawer composition.
 */
import { useCallback, useMemo, type ReactNode } from "react";

import {
  MessageList,
  type MessageListProps,
} from "@/components/chat/MessageList";
import type { ChatMessage } from "@/components/chat/types";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import type { MessageGroup } from "@/hooks/useMessageGrouping";
import { apiFetch } from "@/lib/api-config";
import { cn } from "@/lib/utils";

import {
  AGENT_RUN_LIFECYCLE_CONTENT,
  AGENT_RUN_LIFECYCLE_SUBTYPE,
  AGENT_RUN_PRODUCER_TYPE,
  isAgentRunActivityPayload,
  isAgentRunLifecyclePayload,
  isAgentRunParentControlPayload,
} from "../contracts/agent-run";
import {
  useAgentRunLocalStatusHydration,
  useAgentRunPresentation,
  useAgentRuns,
} from "../hooks/use-agent-run";
import {
  closeAgentRunDrawer,
  openAgentRunList,
} from "../state/agent-run-presentation-store";
import {
  getAgentRunParentGroupingKey,
  type AgentRunRecord,
} from "../state/agent-stream-store";
import { AgentRunCard } from "./AgentRunCard";
import { AgentRunDrawer } from "./AgentRunDrawer";

const AGENT_RUN_TRANSCRIPT_ACTIVITY_KIND = "agent-run";

interface AgentRunTranscriptIntegrationProps
  extends Omit<MessageListProps, "renderActivityGroup"> {
  isConnected: boolean;
}

function readString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function markAgentRunTranscriptActivity(
  message: ChatMessage,
  agentRunId: string,
): ChatMessage {
  return {
    ...message,
    metadata: {
      ...message.metadata,
      transcript_activity_kind: AGENT_RUN_TRANSCRIPT_ACTIVITY_KIND,
      transcript_activity_id: agentRunId,
    },
  };
}

function agentRunIdsForGroup(group: MessageGroup): Set<string> {
  const ids = new Set<string>();
  for (const message of group.messages) {
    const agentRunId = readString(message.metadata?.agent_run_id);
    if (agentRunId) {
      ids.add(agentRunId);
    }
  }
  return ids;
}

function matchingAgentRunsForGroup(
  group: MessageGroup,
  agentRuns: AgentRunRecord[],
): AgentRunRecord[] {
  const agentRunIds = agentRunIdsForGroup(group);
  if (agentRunIds.size === 0) {
    return [];
  }
  return agentRuns.filter((run) => agentRunIds.has(run.agentRunId));
}

function buildParentTranscriptMessages(
  messages: ChatMessage[],
  agentRuns: AgentRunRecord[],
): ChatMessage[] {
  const parentMessages = messages.filter(
    (message) => !isAgentRunActivityPayload(message),
  );
  const existingRunIds = new Set<string>();
  const messagesByTurnId = new Map<string, ChatMessage>();
  for (const message of parentMessages) {
    const agentRunId = readString(message.metadata?.agent_run_id);
    if (agentRunId && isAgentRunLifecyclePayload(message)) {
      existingRunIds.add(agentRunId);
    }
    for (const value of [message.metadata?.id, message.metadata?.turn_id]) {
      const turnId = readString(value);
      if (turnId && !messagesByTurnId.has(turnId)) {
        messagesByTurnId.set(turnId, message);
      }
    }
  }

  const visibleMessages = parentMessages.flatMap((message) => {
    if (isAgentRunParentControlPayload(message)) {
      return [];
    }
    const agentRunId = readString(message.metadata?.agent_run_id);
    if (agentRunId && isAgentRunLifecyclePayload(message)) {
      return [markAgentRunTranscriptActivity(message, agentRunId)];
    }
    return [message];
  });

  const markers: ChatMessage[] = [];
  for (const run of agentRuns) {
    if (existingRunIds.has(run.agentRunId)) {
      continue;
    }
    const parentMessage =
      messagesByTurnId.get(run.parentTurnId) ??
      (run.parentRunId ? messagesByTurnId.get(run.parentRunId) : undefined);
    if (!parentMessage) {
      continue;
    }
    const isWorking = run.status === "running";
    markers.push(
      markAgentRunTranscriptActivity(
        {
          id: `agent-run-marker-${run.agentRunId}`,
          type: "agent",
          content: AGENT_RUN_LIFECYCLE_CONTENT,
          timestamp: new Date(run.createdAt).toISOString(),
          isStreaming: isWorking,
          metadata: {
            id: run.parentTurnId,
            turn_id: run.parentTurnId,
            turn_sequence: parentMessage.metadata?.turn_sequence,
            sequence: run.firstSequence ?? undefined,
            ind: 1,
            step_type: "tool_start",
            subtype: AGENT_RUN_LIFECYCLE_SUBTYPE,
            producer_type: AGENT_RUN_PRODUCER_TYPE,
            agent_run_id: run.agentRunId,
            agent_id: run.agentId,
            agent_kind: run.agentKind,
            agent_display_name: run.agentDisplayName,
            parent_turn_id: run.parentTurnId,
            parent_run_id: run.parentRunId ?? undefined,
            lifecycle_version: run.lifecycleVersion,
            streaming: isWorking,
            is_streaming: isWorking,
            in_progress: isWorking,
          },
        },
        run.agentRunId,
      ),
    );
  }

  return markers.length > 0 ? [...visibleMessages, ...markers] : visibleMessages;
}

function isAgentRunTranscriptGroup(group: MessageGroup): boolean {
  return (
    group.primaryType === "activity" &&
    group.messages.some(
      (message) =>
        message.metadata?.transcript_activity_kind ===
        AGENT_RUN_TRANSCRIPT_ACTIVITY_KIND,
    )
  );
}

export function AgentRunTranscriptIntegration({
  messages,
  taskId,
  isConnected,
  className,
  ...messageListProps
}: AgentRunTranscriptIntegrationProps) {
  useAgentRunLocalStatusHydration(taskId);
  const agentRuns = useAgentRuns(taskId);
  const presentation = useAgentRunPresentation(taskId);
  const parentTranscriptMessages = useMemo(
    () => buildParentTranscriptMessages(messages, agentRuns),
    [agentRuns, messages],
  );
  const activityMessages = useMemo(
    () => messages.filter(isAgentRunActivityPayload),
    [messages],
  );

  const handleOpenAgentRun = useCallback(
    (run: AgentRunRecord) => {
      if (typeof taskId !== "number") return;
      openAgentRunList(taskId, getAgentRunParentGroupingKey(run));
    },
    [taskId],
  );

  const handleStopAgentRun = useCallback(
    async (run: AgentRunRecord) => {
      if (typeof taskId !== "number") return;
      const response = await apiFetch(
        `/api/tasks/${taskId}/agent-runs/${encodeURIComponent(run.agentRunId)}/cancel`,
        { method: "POST" },
      );
      if (!response.ok) {
        throw new Error("Failed to stop subagent run.");
      }
    },
    [taskId],
  );

  const handleCollapseAgentRun = useCallback(() => {
    if (typeof taskId !== "number") return;
    closeAgentRunDrawer(taskId);
  }, [taskId]);

  const renderActivityGroup = useCallback(
    (group: MessageGroup): ReactNode | undefined => {
      if (!isAgentRunTranscriptGroup(group)) {
        return undefined;
      }
      const matchingRuns = matchingAgentRunsForGroup(group, agentRuns);
      if (matchingRuns.length === 0) {
        return null;
      }
      return (
        <div className="flex w-full flex-col gap-1">
          {matchingRuns.map((run) => (
            <AgentRunCard
              key={run.agentRunId}
              run={run}
              onOpen={handleOpenAgentRun}
            />
          ))}
        </div>
      );
    },
    [agentRuns, handleOpenAgentRun],
  );

  return (
    <section
      aria-label="Conversation history"
      className={cn("relative flex h-full min-h-0 flex-col", className)}
    >
      <header className="flex items-center border-b border-slate-800 px-4 py-2 text-[11px] uppercase tracking-wide text-slate-500">
        <span className="font-semibold text-slate-300">Conversation</span>
        <span className="sr-only" aria-live="polite">
          {isConnected ? "Stream connected" : "Stream disconnected"}
        </span>
      </header>

      <div className="relative min-h-0 flex-1">
        <ResizablePanelGroup direction="horizontal" className="h-full min-h-0">
          <ResizablePanel
            id="conversation-panel"
            order={1}
            defaultSize={presentation.isOpen ? 56 : 100}
            minSize={56}
          >
            <MessageList
              {...messageListProps}
              messages={parentTranscriptMessages}
              taskId={taskId}
              renderActivityGroup={renderActivityGroup}
            />
          </ResizablePanel>

          {presentation.isOpen ? (
            <>
              <ResizableHandle
                aria-label="Resize subagents panel"
                className="w-0.5 bg-slate-800/30 transition-colors hover:bg-emerald-500/30"
              />
              <ResizablePanel
                id="subagents-panel"
                order={2}
                defaultSize={44}
                minSize={36}
                maxSize={44}
                collapsible
                collapsedSize={0}
                onCollapse={handleCollapseAgentRun}
                className="min-w-0 overflow-hidden"
              >
                <AgentRunDrawer
                  taskId={taskId}
                  activityMessages={activityMessages}
                  onStopRun={handleStopAgentRun}
                />
              </ResizablePanel>
            </>
          ) : null}
        </ResizablePanelGroup>
      </div>
    </section>
  );
}
