/**
 * Scrollable chat transcript and subagent drawer integration.
 *
 * Responsibilities:
 * - render grouped chat activity without leaking subagent internals
 * - keep subagent drawer navigation behind explicit card/list/detail actions
 * - preserve the existing chat transcript, retry, and scroll behavior
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { ArrowDown, Loader2 } from "lucide-react";

import { AgentRunCard } from "@/features/agent-runs/components/AgentRunCard";
import {
  AgentRunDrawer,
  type AgentRunApprovalControls,
} from "@/features/agent-runs/components/AgentRunDrawer";
import {
  useAgentRunPresentation,
  useAgentRunLocalStatusHydration,
  useAgentRuns,
} from "@/features/agent-runs/hooks/use-agent-run";
import {
  closeAgentRunDrawer,
  getAgentRunParentGroupingKey,
  openAgentRunList,
  type AgentRunRecord,
} from "@/features/agent-runs/state/agent-stream-store";
import { apiFetch } from "@/lib/api-config";
import { cn } from "@/lib/utils";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";

import type { ChatMessage } from "./types";
import { useMessageGrouping } from "@/hooks/useMessageGrouping";
import type { MessageGroup } from "@/hooks/useMessageGrouping";
import { MessageGroupRenderer } from "./MessageGroup";
import type { MessageBubbleRetryState } from "./MessageBubble";
import { TurnActivityCard } from "./TurnActivityCard";
import {
  buildMessageRenderBlocks,
  isAgentRunEventGroup,
} from "./turnActivityBlocks";

export interface MessageListProps {
  messages: ChatMessage[];
  taskId?: number | null;
  isLoading: boolean;
  isConnected: boolean;
  onLoadMore?: () => void | Promise<void>;
  hasMore?: boolean;
  onMessageExpand?: (messageId: string) => void;
  onMessageRetry?: (messageId: string) => void;
  /**
   * Phase 5.3: per-message retry-lifecycle resolver. When provided, the
   * resolver is forwarded to ``MessageBubble`` via ``MessageGroup`` so
   * the retry CTA stays disabled while a backend retry worker is
   * active. Returning ``null`` keeps the legacy server-flag-only
   * behavior.
   */
  resolveRetryState?: (message: ChatMessage) => MessageBubbleRetryState | null;
  autoScrollThreshold?: number;
  emptyState?: ReactNode;
  className?: string;
  agentRunApprovalControls?: AgentRunApprovalControls;
}

const DEFAULT_AUTO_SCROLL_THRESHOLD = 96;
const AGENT_RUN_LIFECYCLE_CONTENT = "agent_run_lifecycle";
const AGENT_RUN_LIFECYCLE_SUBTYPE = "agent_run_lifecycle";

function readString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function isAgentRunLifecycleMessage(message: ChatMessage): boolean {
  const metadata = message.metadata ?? {};
  return (
    readString(metadata.agent_run_id) !== null &&
    readString(metadata.agent_id) !== null &&
    (message.content === AGENT_RUN_LIFECYCLE_CONTENT ||
      metadata.subtype === AGENT_RUN_LIFECYCLE_SUBTYPE)
  );
}

function isAgentRunParentControlMessage(message: ChatMessage): boolean {
  const metadata = message.metadata ?? {};
  return (
    readString(metadata.agent_run_id) !== null &&
    readString(metadata.agent_id) !== null &&
    readString(metadata.agent_kind) !== null &&
    readString(metadata.agent_display_name) !== null &&
    readString(metadata.status) !== null &&
    metadata.producer_type !== "subagent"
  );
}

function isAgentRunActivityMessage(message: ChatMessage): boolean {
  const metadata = message.metadata ?? {};
  const isLifecycle =
    message.content === AGENT_RUN_LIFECYCLE_CONTENT ||
    metadata.subtype === AGENT_RUN_LIFECYCLE_SUBTYPE;
  return (
    !isLifecycle &&
    readString(metadata.agent_run_id) !== null &&
    readString(metadata.agent_id) !== null &&
    metadata.producer_type === "subagent"
  );
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

function addMissingAgentRunMarkers(
  messages: ChatMessage[],
  agentRuns: AgentRunRecord[],
): ChatMessage[] {
  const existingRunIds = new Set<string>();
  const messagesByTurnId = new Map<string, ChatMessage>();
  for (const message of messages) {
    const agentRunId = readString(message.metadata?.agent_run_id);
    if (agentRunId && isAgentRunLifecycleMessage(message)) {
      existingRunIds.add(agentRunId);
    }
    for (const value of [message.metadata?.id, message.metadata?.turn_id]) {
      const turnId = readString(value);
      if (turnId && !messagesByTurnId.has(turnId)) {
        messagesByTurnId.set(turnId, message);
      }
    }
  }

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
    markers.push({
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
        producer_type: "subagent",
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
    });
  }
  const visibleMessages = messages.filter(
    (message) => !isAgentRunParentControlMessage(message),
  );
  return markers.length > 0 ? [...visibleMessages, ...markers] : visibleMessages;
}

export function MessageList({
  messages,
  taskId,
  isLoading,
  isConnected,
  onLoadMore,
  hasMore = false,
  onMessageExpand,
  onMessageRetry,
  resolveRetryState,
  autoScrollThreshold = DEFAULT_AUTO_SCROLL_THRESHOLD,
  emptyState,
  className,
  agentRunApprovalControls,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const topSentinelRef = useRef<HTMLButtonElement | null>(null);
  const bottomAnchorRef = useRef<HTMLDivElement | null>(null);
  const fetchingMoreRef = useRef(false);
  const previousLengthRef = useRef(0);
  const lastMessageIdRef = useRef<string | null>(null);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const [unreadCount, setUnreadCount] = useState(0);

  const handleExpand = useCallback(
    (messageId: string) => onMessageExpand?.(messageId),
    [onMessageExpand],
  );

  const handleRetry = useCallback(
    (messageId: string) => onMessageRetry?.(messageId),
    [onMessageRetry],
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

  const scrollToBottom = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      if (!bottomAnchorRef.current) return;
      bottomAnchorRef.current.scrollIntoView({ behavior });
    },
    [],
  );

  useEffect(() => {
    const previousLength = previousLengthRef.current;
    const lastKnownMessageId = lastMessageIdRef.current;
    const latestMessageId = messages.length
      ? messages[messages.length - 1]?.id ?? null
      : null;

    const appendedNewMessage =
      messages.length > previousLength && lastKnownMessageId !== latestMessageId;

    previousLengthRef.current = messages.length;
    lastMessageIdRef.current = latestMessageId;

    if (messages.length === 0) {
      setUnreadCount(0);
      return;
    }

    if (shouldAutoScroll) {
      requestAnimationFrame(() => {
        scrollToBottom(previousLength ? "smooth" : "auto");
      });
      setUnreadCount(0);
      return;
    }

    if (appendedNewMessage && previousLength > 0) {
      setUnreadCount((current) => current + (messages.length - previousLength));
    }
  }, [messages, shouldAutoScroll, scrollToBottom]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      const distanceFromBottom = scrollHeight - (scrollTop + clientHeight);
      const isNearBottom = distanceFromBottom <= autoScrollThreshold;

      setShouldAutoScroll((prev) => (prev !== isNearBottom ? isNearBottom : prev));
      if (isNearBottom) {
        setUnreadCount(0);
      }
    };

    container.addEventListener("scroll", handleScroll, { passive: true });
    handleScroll();

    return () => {
      container.removeEventListener("scroll", handleScroll);
    };
  }, [autoScrollThreshold]);

  const handleLoadMore = useCallback(() => {
    if (!onLoadMore || fetchingMoreRef.current) return;
    fetchingMoreRef.current = true;

    Promise.resolve(onLoadMore())
      .catch(() => undefined)
      .finally(() => {
        setTimeout(() => {
          fetchingMoreRef.current = false;
        }, 200);
      });
  }, [onLoadMore]);

  useEffect(() => {
    if (!hasMore || !onLoadMore) return;
    const sentinel = topSentinelRef.current;
    const container = containerRef.current;
    if (!sentinel || !container) return;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            handleLoadMore();
          }
        });
      },
      {
        root: container,
        rootMargin: "120px 0px 0px 0px",
        threshold: 0.01,
      },
    );

    observer.observe(sentinel);

    return () => observer.disconnect();
  }, [hasMore, onLoadMore, handleLoadMore]);

  useAgentRunLocalStatusHydration(taskId);
  const agentRuns = useAgentRuns(taskId);
  const agentRunPresentation = useAgentRunPresentation(taskId);

  // Group messages by `ind` field for proper rendering.
  const parentTranscriptMessages = useMemo(() => {
    const parentMessages = messages.filter(
      (message) => !isAgentRunActivityMessage(message),
    );
    return addMissingAgentRunMarkers(parentMessages, agentRuns);
  }, [agentRuns, messages]);
  const agentRunActivityMessages = useMemo(
    () => messages.filter(isAgentRunActivityMessage),
    [messages],
  );
  const messageGroups = useMessageGrouping(parentTranscriptMessages);
  const renderBlocks = useMemo(() => buildMessageRenderBlocks(messageGroups), [messageGroups]);

  const renderActivityGroup = useCallback(
    (group: MessageGroup): ReactNode | undefined => {
      if (!isAgentRunEventGroup(group)) {
        return undefined;
      }
      const matchingRuns = matchingAgentRunsForGroup(group, agentRuns);
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
  
  const renderedMessages = useMemo(
    () =>
      renderBlocks.map((block, index) => {
        if (block.type === "activity") {
          const firstMessage = block.groups[0]?.messages[0];
          return (
            <li
              key={block.key}
              className="flex"
              data-testid={`chat-message-${index}`}
              data-group-type="activity"
              data-turn-sequence={firstMessage?.metadata?.turn_sequence ?? ""}
            >
              <TurnActivityCard
                block={block}
                taskId={taskId}
                onGroupExpand={handleExpand}
                onGroupRetry={handleRetry}
                renderGroup={renderActivityGroup}
              />
            </li>
          );
        }

        const { group } = block;
        if (isAgentRunEventGroup(group)) {
          const matchingRuns = matchingAgentRunsForGroup(group, agentRuns);
          if (matchingRuns.length === 0) {
            return null;
          }
          return (
            <li
              key={block.key}
              className="flex"
              data-testid={`chat-message-${index}`}
              data-group-type="agent-run"
              data-turn-sequence={group.messages[0]?.metadata?.turn_sequence ?? ""}
            >
              <div className="flex w-full flex-col gap-1">
                {matchingRuns.map((run) => (
                  <AgentRunCard
                    key={run.agentRunId}
                    run={run}
                    onOpen={handleOpenAgentRun}
                  />
                ))}
              </div>
            </li>
          );
        }
        // Use stable group key when available
        const firstMessage = group.messages[0];
        const key = block.key ?? firstMessage?.id ?? `group-${group.ind}-${index}`;
        
        return (
          <li
            key={key}
            className="flex"
            data-testid={`chat-message-${index}`}
            data-group-type={group.primaryType}
            data-turn-sequence={firstMessage?.metadata?.turn_sequence ?? ""}
          >
            <MessageGroupRenderer
              group={group}
              taskId={taskId}
              onToggleExpand={() => firstMessage && handleExpand(firstMessage.id)}
              onRetry={() => firstMessage && handleRetry(firstMessage.id)}
              resolveRetryState={resolveRetryState}
            />
          </li>
        );
      }),
    [
      agentRuns,
      handleExpand,
      handleOpenAgentRun,
      handleRetry,
      renderActivityGroup,
      renderBlocks,
      resolveRetryState,
      taskId,
    ],
  );

  const resolvedEmptyState = emptyState ?? (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center text-sm text-slate-400">
      <p className="font-medium text-slate-300">No messages yet</p>
      <p className="max-w-sm text-xs text-slate-500">
        Interactions and reasoning steps will appear here once the agent begins processing the task.
      </p>
    </div>
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
            defaultSize={agentRunPresentation.isOpen ? 56 : 100}
            minSize={56}
          >
            <div
              ref={containerRef}
              role="log"
              aria-live="polite"
              aria-busy={isLoading}
              data-testid="chat-message-list"
              className="relative h-full min-w-0 overflow-y-auto overflow-x-hidden px-4 py-4"
            >
        {hasMore && (
          <div className="flex justify-center pb-2 text-xs" data-testid="message-list-load-more">
            <button
              ref={topSentinelRef}
              type="button"
              onClick={handleLoadMore}
              className={cn(
                "inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-slate-200 transition",
                "hover:border-slate-600 hover:bg-slate-900/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500",
              )}
              disabled={fetchingMoreRef.current}
              aria-label="Load previous messages"
            >
              {fetchingMoreRef.current && (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              )}
              {fetchingMoreRef.current ? "Loading…" : "Load previous"}
            </button>
          </div>
        )}

        <div data-testid="reasoning-pane" className="contents">
        {isLoading && messages.length === 0 ? (
          <div className="flex items-center justify-center py-10 text-slate-400">
            <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
          </div>
        ) : messages.length === 0 ? (
          resolvedEmptyState
        ) : (
          <ul className="flex flex-col gap-2" aria-live="polite">
            {renderedMessages}
          </ul>
        )}
        </div>

          <div ref={bottomAnchorRef} aria-hidden="true" />
            </div>
          </ResizablePanel>

          {agentRunPresentation.isOpen ? (
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
                  activityMessages={agentRunActivityMessages}
                  approvalControls={agentRunApprovalControls}
                  onStopRun={handleStopAgentRun}
                />
              </ResizablePanel>
            </>
          ) : null}
        </ResizablePanelGroup>
      </div>

      {unreadCount > 0 && (
        <button
          type="button"
          onClick={() => {
            scrollToBottom();
            setShouldAutoScroll(true);
            setUnreadCount(0);
          }}
          className="absolute bottom-6 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 rounded-full border border-indigo-400/60 bg-indigo-500/20 px-3 py-1 text-xs font-medium text-indigo-100 shadow-lg backdrop-blur transition hover:border-indigo-300 hover:bg-indigo-500/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
          aria-label={`Jump to latest messages (${unreadCount} unread)`}
        >
          <ArrowDown className="h-4 w-4" aria-hidden="true" />
          {unreadCount} unread
        </button>
      )}

    </section>
  );
}

export default MessageList;
