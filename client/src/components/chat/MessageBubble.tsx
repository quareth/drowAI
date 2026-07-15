import { useMemo } from "react";
import type { JSX } from "react";
import { Loader2, RefreshCcw, Sparkles, AlertCircle, Maximize2, ShieldAlert } from "lucide-react";

import { cn } from "@/lib/utils";
import { MarkdownMessage } from "@/components/ui/markdown-message";
import { StreamingContent } from "@/components/ui/streaming-content";
import { useUserTimezone } from "@/hooks/use-user-timezone";
import { formatTime } from "@/utils/datetime";

import type { ChatMessage } from "./types";
import { RefusalNotice } from "./RefusalNotice";

/**
 * Lifecycle snapshot for a retryable failed assistant turn.
 *
 * Mirrors the public shape of ``TaskRetryStateEntry`` in
 * ``@/state/retry-state-store`` but is duplicated here so
 * ``MessageBubble`` stays presentational and does not import the store
 * directly (Phase 5 contract: chat components consume retry state via
 * props, not via cross-module imports).
 */
export interface MessageBubbleRetryState {
  taskId: number;
  turnId: string;
  workflowId: number | null;
  state:
    | "accepted"
    | "started"
    | "retrying"
    | "waiting_for_human"
    | "completed"
    | "declined"
    | "failed"
    | "cancelled";
  retryAttempt: number | null;
  retryMaxAttempts: number | null;
  inFlight: boolean;
}

export interface MessageBubbleProps {
  message: ChatMessage;
  isStreaming?: boolean;
  onExpand?: (messageId: string) => void;
  onRetry?: (messageId: string) => void;
  /**
   * Optional retry lifecycle for the assistant turn this bubble
   * represents. When present, it controls whether the retry CTA is
   * disabled regardless of click intent — duplicate clicks must NOT
   * issue another POST while the backend retry worker is active.
   */
  retryState?: MessageBubbleRetryState | null;
}

const PRESENTATION: Record<ChatMessage["type"], {
  alignment: string;
  wrapper: string;
  bubbleClass: string;
  label?: string;
}> = {
  user: {
    alignment: "justify-end",
    wrapper: "items-end text-left",
    bubbleClass: "bg-slate-900/50 text-slate-200",
    label: "You",
  },
  agent: {
    alignment: "justify-start",
    wrapper: "items-start text-left w-full",
    bubbleClass: "bg-slate-900/50 text-slate-200",
  },
  system: {
    alignment: "justify-center",
    wrapper: "items-center text-center",
    bubbleClass: "bg-slate-900/80 text-slate-300 border border-slate-800",
  },
  thinking: {
    alignment: "justify-start",
    wrapper: "items-start text-left",
    bubbleClass: "border border-amber-600/40 bg-amber-500/10 text-amber-100",
    label: "Thinking",
  },
  executing: {
    alignment: "justify-start",
    wrapper: "items-start text-left",
    bubbleClass: "border border-emerald-500/40 bg-emerald-500/10 text-emerald-100",
    label: "Executing",
  },
};

const BASE_BUBBLE_CLASS =
  "relative inline-block max-w-[70%] rounded-2xl px-3 py-1.5 text-left text-sm shadow-none transition-colors break-words overflow-wrap-anywhere";
const STATUS_BADGE_CLASS =
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium";

function renderStatusBadge(status?: string): JSX.Element | null {
  if (!status) return null;
  const normalized = status.toLowerCase();
  if (normalized === "error") {
    return (
      <span className={cn(STATUS_BADGE_CLASS, "border-rose-500/50 bg-rose-500/10 text-rose-200")}>
        <AlertCircle className="h-3 w-3" aria-hidden="true" />
        Error
      </span>
    );
  }
  if (normalized === "declined") {
    return (
      <span className={cn(STATUS_BADGE_CLASS, "border-amber-500/50 bg-amber-500/10 text-amber-100")}>
        <ShieldAlert className="h-3 w-3" aria-hidden="true" />
        Declined
      </span>
    );
  }
  return null;
}

export function MessageBubble({ message, isStreaming, onExpand, onRetry, retryState }: MessageBubbleProps) {
  const timezone = useUserTimezone();
  const streaming = isStreaming ?? message.isStreaming ?? false;
  const { type, content, timestamp, metadata } = message;
  const presentation = PRESENTATION[type];
  const isUserMessage = type === "user";
  const isSystemMessage = type === "system";
  const isDeclined = metadata?.status === "declined";
  // Route through the plain-text renderer for any message that originated
  // from a failure, not just messages whose *current* workflow projection
  // status is "error". The persisted ``error_code`` survives when the
  // workflow row transitions FAILED → RETRYING / WAITING_FOR_HUMAN /
  // COMPLETED while the chat message content is still ``[Error] …``;
  // without this guard the streaming JSON detector trips on the leading
  // ``[`` and renders the static error string inside a broken JSON
  // container.
  const isErrorMessage =
    metadata?.status === "error" ||
    (typeof metadata?.error_code === "string" && metadata.error_code.trim().length > 0);

  const timeLabel = useMemo(() => {
    const formatted = formatTime(timestamp, timezone);
    return formatted === "—" ? "" : formatted;
  }, [timestamp, timezone]);
  const showTimestamp = Boolean(timeLabel);
  const showExpand = Boolean(metadata?.canExpand && onExpand);
  const showRetry = Boolean(
    metadata?.status === "error" &&
      metadata?.retryable === true &&
      onRetry,
  );
  // Phase 5.3: the retry CTA disables while a retry lifecycle is in
  // flight or has settled into a state that should not re-arm the
  // button. ``failed`` is the only terminal state that re-enables the
  // button, and only when the message is still server-marked
  // ``retryable`` (i.e. the backend retry budget still has room).
  // Additional terminal states (``completed``, ``cancelled``) keep the
  // button disabled — the user has no further retry CTA to click.
  const retryDisabled = Boolean(
    retryState &&
      (retryState.inFlight ||
        retryState.state === "completed" ||
        retryState.state === "declined" ||
        retryState.state === "cancelled"),
  );

  if (isSystemMessage) {
    return (
      <section
        role="status"
        aria-live="polite"
        data-testid="message-bubble-system"
        data-message-type={type}
        className="flex justify-center py-3"
      >
        <div className="flex max-w-2xl flex-col items-center gap-2 text-xs text-slate-400">
          <div
            className={cn(BASE_BUBBLE_CLASS, presentation.bubbleClass, "max-w-xl text-center")}
            aria-label="System message"
          >
            <MarkdownMessage content={content} />
          </div>
        </div>
      </section>
    );
  }

  const showHeader = Boolean(
    (isUserMessage && presentation.label) ||
      metadata?.stepType ||
      showTimestamp ||
      (!isUserMessage && metadata?.status), // Don't show status for user messages
  );

  const containerClasses = cn("flex w-full gap-3 py-2", presentation.alignment);
  const wrapperClasses = cn("flex w-full flex-col gap-2", presentation.wrapper);

  const bubbleClasses = cn(
    BASE_BUBBLE_CLASS,
    presentation.bubbleClass,
    isDeclined && "max-w-[80%] border border-amber-500/30 bg-transparent text-amber-50",
    isUserMessage ? "ml-auto" : "mr-auto",
    streaming && isUserMessage && "border-slate-600/60 shadow-inner shadow-slate-500/10",
  );

  return (
    <article
      tabIndex={0}
      data-testid={`message-bubble-${type}`}
      data-message-id={message.id}
      data-message-type={type}
      className={containerClasses}
    >
      <div className={wrapperClasses}>
        {showHeader && (
          <header className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-wide text-slate-500">
            {isUserMessage && presentation.label && (
              <span className="font-semibold text-slate-200">{presentation.label}</span>
            )}
            {metadata?.stepType && (
              <span className="rounded-full border border-slate-800 bg-slate-900/60 px-2 py-0.5 text-[10px] lowercase text-slate-400">
                {metadata.stepType}
              </span>
            )}
            {showTimestamp && (
              <time dateTime={timestamp} className="font-medium text-slate-500">
                {timeLabel}
              </time>
            )}
            {!isUserMessage && renderStatusBadge(metadata?.status)}
          </header>
        )}

        <div className={bubbleClasses} aria-live={streaming ? "polite" : undefined}>
          {metadata?.command && (
            <div className="mb-2 flex items-center gap-2 text-xs font-mono uppercase tracking-wide text-indigo-200">
              <Sparkles className="h-3 w-3 text-indigo-300" aria-hidden="true" />
              {metadata.command}
            </div>
          )}
          <div className="break-words overflow-wrap-anywhere">
            {isDeclined ? (
              <div className="space-y-3">
                {metadata?.refusal?.partial && content.trim().length > 0 && (
                  <section
                    aria-label="Incomplete response"
                    className="rounded-xl border border-slate-700/70 bg-slate-900/60 p-3 text-slate-200"
                  >
                    <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-amber-300">
                      Incomplete response
                    </div>
                    <StreamingContent content={content} isStreaming={false} />
                  </section>
                )}
                <RefusalNotice
                  refusal={metadata?.refusal}
                  fallbackSummary={content}
                />
              </div>
            ) : isUserMessage ? (
              <MarkdownMessage content={content} />
            ) : isErrorMessage ? (
              <MarkdownMessage content={content} />
            ) : (
              <StreamingContent content={content} isStreaming={streaming} />
            )}
          </div>
          {streaming && !isErrorMessage && !isDeclined && (
            <div className="mt-3 flex items-center gap-2 text-xs text-indigo-200">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              Streaming response…
            </div>
          )}
        </div>

        {(showExpand || showRetry) && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {showExpand && (
              <button
                type="button"
                onClick={() => onExpand?.(message.id)}
                className="inline-flex items-center gap-1 rounded-full border border-slate-700 bg-slate-900/70 px-2.5 py-1 text-slate-200 transition hover:border-slate-600 hover:bg-slate-900"
                aria-label="Expand message details"
              >
                <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
                View details
              </button>
            )}
            {showRetry && (
              <button
                type="button"
                onClick={() => {
                  // Defensive guard: even if the ``disabled`` attribute
                  // is somehow bypassed (e.g. assistive tooling or a
                  // raced re-render), do not issue another retry POST
                  // while the lifecycle is active or settled into a
                  // non-retryable terminal state.
                  if (retryDisabled) {
                    return;
                  }
                  onRetry?.(message.id);
                }}
                disabled={retryDisabled}
                aria-disabled={retryDisabled}
                className="inline-flex items-center gap-1 rounded-full border border-rose-600/60 bg-rose-500/10 px-2.5 py-1 text-rose-100 transition hover:border-rose-500 hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-rose-600/60 disabled:hover:bg-rose-500/10"
                aria-label="Retry action"
              >
                <RefreshCcw className="h-3.5 w-3.5" aria-hidden="true" />
                Retry
              </button>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

export default MessageBubble;
