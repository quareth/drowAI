/**
 * Minimal tool execution card rendering normalized tool phase data.
 *
 * Displays the human-friendly tool name and status, then resolves/presents
 * persisted terminal raw output from provenance APIs.
 */

import { Loader2, Wrench, Copy } from "lucide-react";

import { formatToolName } from "@/utils/toolName";
import { useToast } from "@/hooks/use-toast";
import { useCardToggleState } from "@/hooks/useCardToggleState";
import { useToolRawOutput } from "@/components/chat/tool-card-terminal/useToolRawOutput";
import { ToolCardTerminalOutput } from "@/components/chat/tool-card-terminal/ToolCardTerminalOutput";
import type { ToolRawOutputNotAvailableReason } from "@/components/chat/tool-card-terminal/toolRawOutput.types";
import type { CompactToolResult } from "@/types/compact-tool-result";
import { cn } from "@/lib/utils";
import {
  deriveShellLifecycleLabels,
  type ToolLifecycleStatus,
} from "./toolLifecycleStatus";

interface ExecutingToolCardProps {
  toolName: string;
  status?: ToolLifecycleStatus;
  sessionStatus?: "active" | "closed" | "unavailable" | string;
  processStatus?: "running" | "completed" | "terminated" | "timed_out" | "failed" | string;
  interactionBoundary?: "output_available" | "quiet_boundary" | "terminal" | string;
  taskId?: number | string;
  toolCallId?: string;
  /** Stable identifier so collapse/expand state persists across remounts. */
  stateKey?: string;
  /** Stable selector for deterministic E2E assertions. */
  testId?: string;
  /** Current retry attempt number (1-indexed). */
  retryAttempt?: number;
  /** Maximum retry attempts allowed. */
  retryMaxAttempts?: number;
  /** Layout mode used when the card is embedded inside a multi-tool batch. */
  layout?: "standalone" | "batch-row";
  /** Same-turn compact output for calls whose raw artifacts are intentionally transient. */
  compactToolResult?: CompactToolResult | null;
  /** Sanitized command text carried by the live shell tool-start event. */
  commandDisplay?: string;
  /** Accumulated same-turn output from correlated shell lifecycle deltas. */
  transientOutput?: string;
}

function formatCompactOutput(
  result: CompactToolResult | null | undefined,
  commandDisplay?: string,
): string {
  if (!result) return "";
  const command = String(commandDisplay || "").trim();
  const summary = typeof result.summary === "string" ? result.summary : "";
  const keyFindings = Array.isArray(result.key_findings) ? result.key_findings : [];
  const errors = Array.isArray(result.errors) ? result.errors : [];
  const lines = [command ? `$ ${command}` : "", summary, ...keyFindings, ...errors]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  return Array.from(new Set(lines)).join("\n");
}

function formatTransientOutput(output: string | undefined, commandDisplay?: string): string {
  const command = String(commandDisplay || "").trim();
  const body = String(output || "");
  if (!body) return "";
  return command ? `$ ${command}\n${body}` : body;
}

function getUnavailableMessage(reason: ToolRawOutputNotAvailableReason): string {
  switch (reason) {
    case "execution_not_found":
      return "Raw output unavailable: execution record not found.";
    case "missing_output_artifacts":
      return "Raw output unavailable: terminal output artifacts were not persisted.";
    case "artifact_not_found":
      return "Raw output unavailable: referenced artifact data is missing.";
    case "artifact_content_unavailable":
      return "Raw output unavailable: artifact content is omitted or non-text.";
    case "missing_identifiers":
    default:
      return "Raw output unavailable: missing task or tool call metadata.";
  }
}

export function ExecutingToolCard({
  toolName,
  status = "executing",
  sessionStatus,
  processStatus,
  interactionBoundary,
  taskId,
  toolCallId,
  stateKey,
  testId,
  retryAttempt,
  retryMaxAttempts,
  layout = "standalone",
  compactToolResult,
  commandDisplay,
  transientOutput,
}: ExecutingToolCardProps) {
  const { toast } = useToast();
  const [isOpen, setIsOpen] = useCardToggleState(stateKey, false);
  const normalizedToolCallId = typeof toolCallId === "string" ? toolCallId.trim() : "";
  const hasLookupIdentity = taskId != null && normalizedToolCallId.length > 0;
  const compactOutputText = formatCompactOutput(compactToolResult, commandDisplay);
  const streamedOutputText = formatTransientOutput(transientOutput, commandDisplay);
  const inlineOutputText = streamedOutputText || compactOutputText;
  const hasInlineOutput = inlineOutputText.length > 0;

  const isExecuting = status === "executing";
  const isCompleted = status === "completed";
  const isFailed = status === "failed";
  const isCancelled = status === "cancelled";
  const isTerminated = status === "terminated";
  const isTimedOut = status === "timed_out";

  const statusLabel = isExecuting
    ? "Running"
    : isCompleted
      ? "Completed"
      : isTimedOut
        ? "Timed out"
        : isCancelled || isTerminated
          ? "Stopped"
          : "Failed";
  const statusColor = isExecuting
    ? "text-slate-400"
    : isCompleted
      ? "text-emerald-400"
      : isCancelled || isTerminated
        ? "text-slate-400"
        : "text-rose-400";

  const rawOutput = useToolRawOutput({
    taskId,
    toolCallId,
    enabled: !isExecuting && isOpen && hasLookupIdentity && !hasInlineOutput,
  });
  const canExpand = hasInlineOutput || (!isExecuting && hasLookupIdentity);
  const {
    processLabel: sessionProcessLabel,
    activityLabel: sessionActivityLabel,
  } = deriveShellLifecycleLabels({
    sessionStatus,
    processStatus,
    interactionBoundary,
  });

  const readyOutputText = hasInlineOutput
    ? inlineOutputText
    : rawOutput.state.status === "ready"
      ? rawOutput.state.outputText
      : "";
  const hasCopyableOutput = isOpen && readyOutputText.length > 0;
  const copyRawOutput = () => {
    if (!hasCopyableOutput) return;
    navigator.clipboard.writeText(readyOutputText);
    toast({ title: "Copied to clipboard" });
  };
  const isBatchRow = layout === "batch-row";
  const cardClassName = cn(
    "rounded-lg border border-transparent bg-slate-950/40 overflow-hidden min-w-0",
    isBatchRow
      ? "block w-full max-w-full"
      : isOpen
        ? "mb-1 mr-auto block w-full max-w-[calc(100%-2rem)]"
        : "mb-1 mr-auto inline-block max-w-[calc(100%-2rem)]",
  );

  return (
    <div
      data-testid={testId}
      className={cardClassName}
    >
      {/* Header - Clickable to toggle (no visible chevron for minimalist style) */}
      <div className="flex w-full min-w-0 items-center">
        <button
          type="button"
          aria-label="Toggle tool output"
          aria-expanded={canExpand ? isOpen : undefined}
          disabled={!canExpand}
          onClick={() => {
            if (canExpand) {
              setIsOpen(!isOpen);
            }
          }}
          className="flex min-w-0 flex-1 items-center gap-2 px-3 py-1.5 text-left transition-colors hover:bg-slate-900/60 disabled:cursor-not-allowed disabled:hover:bg-transparent"
        >
          {/* Icon */}
          {isExecuting ? (
            <Loader2 className="w-3 h-3 text-slate-500 animate-spin shrink-0" />
          ) : (
            <Wrench
              className={`w-3 h-3 shrink-0 ${
                isCompleted ? "text-emerald-500" : isFailed || isTimedOut ? "text-rose-500" : "text-slate-500"
              }`}
            />
          )}

          {/* Tool name + status */}
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-slate-400">
            {formatToolName(toolName)}
          </span>
          <span className={`shrink-0 whitespace-nowrap text-[10px] font-medium ${statusColor}`}>{statusLabel}</span>
          {sessionProcessLabel && (
            <span className="shrink-0 whitespace-nowrap text-[10px] font-medium text-sky-400">
              {sessionProcessLabel}
            </span>
          )}
          {sessionActivityLabel && (
            <span className="shrink-0 whitespace-nowrap text-[10px] font-medium text-slate-400">
              {sessionActivityLabel}
            </span>
          )}

          {/* Retry badge */}
          {retryAttempt && retryMaxAttempts && (
            <span className="shrink-0 whitespace-nowrap rounded bg-amber-950/30 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
              Attempt {retryAttempt}/{retryMaxAttempts}
            </span>
          )}
        </button>

        {/* Copy raw output (compact icon, only when output is visible) */}
        {hasCopyableOutput && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              copyRawOutput();
            }}
            className="mr-2 shrink-0 rounded p-1 text-slate-400 transition-colors hover:bg-slate-800/50 hover:text-slate-200"
            title="Copy raw output"
            aria-label="Copy raw output"
          >
            <Copy className="w-3 h-3" />
          </button>
        )}
      </div>

      {/* Content - tool output */}
      {(isOpen || (isExecuting && hasInlineOutput)) && (
        <div className="min-w-0 max-w-full border-t border-slate-800/70 bg-slate-950/80 px-3 py-2">
          {hasInlineOutput && (
            <ToolCardTerminalOutput
              outputText={inlineOutputText}
              isExpanded={isOpen || isExecuting}
              isReady
              testId={testId ? `${testId}-terminal` : undefined}
            />
          )}
          {!hasInlineOutput && rawOutput.state.status === "loading" && (
            <div className="flex items-center gap-2 text-xs text-slate-400/90 font-mono">
              <Loader2 className="w-3 h-3 animate-spin" />
              <span>Loading raw output...</span>
            </div>
          )}
          {!hasInlineOutput && rawOutput.state.status === "ready" && (
            <ToolCardTerminalOutput
              outputText={rawOutput.state.outputText}
              isExpanded={isOpen}
              isReady
              testId={testId ? `${testId}-terminal` : undefined}
            />
          )}
          {!hasInlineOutput && rawOutput.state.status === "not_available" && (
            <p className="font-mono text-xs text-slate-400/90 leading-relaxed">
              {getUnavailableMessage(rawOutput.state.reason)}
            </p>
          )}
          {!hasInlineOutput && rawOutput.state.status === "error" && (
            <p className="font-mono text-xs text-slate-400/90 leading-relaxed">
              Raw output unavailable due to a retrieval error.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
