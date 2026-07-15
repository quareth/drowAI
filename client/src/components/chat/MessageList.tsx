import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { ArrowDown, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

import type { ChatMessage } from "./types";
import { useMessageGrouping } from "@/hooks/useMessageGrouping";
import { MessageGroupRenderer } from "./MessageGroup";
import type { MessageBubbleRetryState } from "./MessageBubble";
import { TurnActivityCard } from "./TurnActivityCard";
import { buildMessageRenderBlocks } from "./turnActivityBlocks";

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
}

const DEFAULT_AUTO_SCROLL_THRESHOLD = 96;

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

  // Group messages by `ind` field for proper rendering
  const messageGroups = useMessageGrouping(messages);
  const renderBlocks = useMemo(() => buildMessageRenderBlocks(messageGroups), [messageGroups]);
  
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
              />
            </li>
          );
        }

        const { group } = block;
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
    [renderBlocks, taskId, handleExpand, handleRetry, resolveRetryState],
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

      <div
        ref={containerRef}
        role="log"
        aria-live="polite"
        aria-busy={isLoading}
        data-testid="chat-message-list"
        className="relative flex-1 overflow-y-auto overflow-x-hidden px-4 py-4"
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
