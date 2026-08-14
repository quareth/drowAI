/**
 * Renders a tool batch as one card with per-call rows.
 *
 * Phase 7 Task 7.5: each row reuses ``ExecutingToolCard`` so the per-row raw-
 * output expansion uses the existing ``useToolRawOutput`` hook keyed by
 * ``tool_call_id``. Rows are presented in manifest order — the manifest is
 * derived from the first ``tool_batch_start`` event when present and falls
 * back to first-seen order across the per-tool events. Failed/cancelled rows
 * are visually distinguished via ``ExecutingToolCard``'s ``status`` prop.
 *
 * The batch header surfaces strategy + aggregate status + progress count so
 * users see at a glance whether the batch is still in flight, partially
 * completed, fully cancelled, or fully successful. The single-tool case
 * (``items.length === 1``) renders identically to ``ExecutingToolCard`` to
 * preserve the legacy single-call appearance under the cap-flip rollout.
 */

import { useMemo } from "react";

import { ExecutingToolCard } from "./ExecutingToolCard";
import {
  deriveToolLifecycleStatus,
  type ToolLifecycleStatus,
} from "./toolLifecycleStatus";
import type { ChatMessage } from "./types";
import type { CompactToolResult } from "@/types/compact-tool-result";

interface ToolBatchCardProps {
  /** All messages grouped under one ``tool_batch_id``. */
  messages: ChatMessage[];
  /** Stable group key used to derive nested per-row stateKey + testId. */
  groupKey: string;
  taskId?: number | string | null;
}

interface BatchRowState {
  toolCallId: string;
  toolName: string;
  commandDisplay?: string;
  status: ToolLifecycleStatus;
  sessionStatus?: string;
  processStatus?: string;
  interactionBoundary?: string;
  retryAttempt?: number;
  retryMaxAttempts?: number;
  compactToolResult?: CompactToolResult;
  shellOutputChunks?: string[];
  /** First-seen index in the message stream — used as a stable order key when
   * the batch_start manifest is absent. */
  firstSeenIndex: number;
  /** Manifest order index from the originating ``tool_batch_start`` event,
   * when present. Takes precedence over ``firstSeenIndex``. */
  manifestIndex?: number;
}

interface BatchAggregate {
  status: "executing" | "completed" | "completed_with_errors" | "failed" | "cancelled" | "denied" | "unknown";
  strategy?: string;
}

function getStepType(message: ChatMessage): string | undefined {
  const stepType = message.metadata?.step_type ?? (message.metadata as any)?.stepType;
  return typeof stepType === "string" ? stepType : undefined;
}

function readString(metadata: Record<string, unknown> | undefined, key: string): string | undefined {
  const value = metadata?.[key];
  if (typeof value === "string" && value.trim().length > 0) {
    return value;
  }
  return undefined;
}

function readNumber(metadata: Record<string, unknown> | undefined, key: string): number | undefined {
  const value = metadata?.[key];
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return undefined;
}

function buildManifestIndex(messages: ChatMessage[]): Map<string, number> {
  for (const msg of messages) {
    if (getStepType(msg) !== "tool_batch_start") continue;
    const items = (msg.metadata as Record<string, unknown> | undefined)?.tool_calls;
    if (!Array.isArray(items)) continue;
    const order = new Map<string, number>();
    items.forEach((entry, idx) => {
      if (entry && typeof entry === "object") {
        const callId = (entry as Record<string, unknown>).tool_call_id;
        if (typeof callId === "string" && callId.length > 0) {
          order.set(callId, idx);
        }
      }
    });
    if (order.size > 0) return order;
  }
  return new Map();
}

function buildRows(messages: ChatMessage[], manifest: Map<string, number>): BatchRowState[] {
  const rows = new Map<string, BatchRowState>();
  let firstSeen = 0;

  messages.forEach((msg) => {
    const stepType = getStepType(msg);
    if (!stepType?.startsWith("tool")) return;

    const metadata = msg.metadata as Record<string, unknown> | undefined;
    if (stepType === "tool_batch_start") {
      const items = metadata?.tool_calls;
      if (Array.isArray(items)) {
        items.forEach((entry) => {
          if (!entry || typeof entry !== "object") return;
          const item = entry as Record<string, unknown>;
          const callId = typeof item.tool_call_id === "string" ? item.tool_call_id : "";
          if (!callId) return;
          if (!rows.has(callId)) {
            const toolName =
              (typeof item.tool_id === "string" && item.tool_id) ||
              (typeof item.tool === "string" && item.tool) ||
              "unknown";
            rows.set(callId, {
              toolCallId: callId,
              toolName,
              status: "executing",
              firstSeenIndex: firstSeen++,
              manifestIndex: manifest.get(callId),
            });
          }
        });
      }
      return;
    }

    if (stepType === "tool_batch_end") {
      const results = metadata?.results;
      if (Array.isArray(results)) {
        results.forEach((entry) => {
          if (!entry || typeof entry !== "object") return;
          const item = entry as Record<string, unknown>;
          const callId = typeof item.tool_call_id === "string" ? item.tool_call_id : "";
          if (!callId) return;
          let row = rows.get(callId);
          if (!row) {
            row = {
              toolCallId: callId,
              toolName:
                (typeof item.tool_id === "string" && item.tool_id) ||
                (typeof item.tool === "string" && item.tool) ||
                "unknown",
              status: "executing",
              firstSeenIndex: firstSeen++,
              manifestIndex: manifest.get(callId),
            };
            rows.set(callId, row);
          }
          const rawStatus = typeof item.status === "string" ? item.status : undefined;
          const processStatus =
            typeof item.process_status === "string" ? item.process_status : undefined;
          const effectiveProcessStatus = processStatus ?? row.processStatus;
          if (processStatus) {
            row.processStatus = processStatus;
          }
          const sessionStatus =
            typeof item.session_status === "string" ? item.session_status : undefined;
          if (sessionStatus) {
            row.sessionStatus = sessionStatus;
          }
          const interactionBoundary =
            typeof item.interaction_boundary === "string" ? item.interaction_boundary : undefined;
          if (interactionBoundary) {
            row.interactionBoundary = interactionBoundary;
          }
          row.status = deriveToolLifecycleStatus(rawStatus, effectiveProcessStatus);
        });
      }
      return;
    }

    const callId = readString(metadata, "tool_call_id");
    if (!callId) return;

    let row = rows.get(callId);
    if (!row) {
      row = {
        toolCallId: callId,
        toolName: "unknown",
        status: "executing",
        firstSeenIndex: firstSeen++,
        manifestIndex: manifest.get(callId),
      };
      rows.set(callId, row);
    }

    const toolName =
      readString(metadata, "tool_name") ??
      readString(metadata, "tool") ??
      readString(metadata, "command");
    if (toolName) {
      row.toolName = toolName;
    }
    const commandDisplay = readString(metadata, "command_display");
    if (commandDisplay) {
      row.commandDisplay = commandDisplay;
    }

    const retryAttempt = readNumber(metadata, "retry_attempt");
    if (retryAttempt !== undefined) {
      row.retryAttempt = retryAttempt;
      row.retryMaxAttempts = readNumber(metadata, "retry_max_attempts");
    }

    if (stepType === "tool_delta" || stepType === "tool_end") {
      const processStatus = readString(metadata, "process_status");
      row.status = deriveToolLifecycleStatus(readString(metadata, "status"), processStatus);
      row.processStatus = processStatus ?? row.processStatus;
      row.sessionStatus = readString(metadata, "session_status") ?? row.sessionStatus;
      row.interactionBoundary =
        readString(metadata, "interaction_boundary") ?? row.interactionBoundary;
      if (metadata?.output_persistence === "transient") {
        const isShellLifecycle = metadata.shell_lifecycle_event === true;
        const isShellOutputChunk =
          (stepType === "tool_delta" &&
            readString(metadata, "interaction_boundary") === "output_available") ||
          (stepType === "tool_end" && metadata.shell_output_chunk === true);
        if (
          isShellLifecycle &&
          isShellOutputChunk &&
          typeof msg.content === "string" &&
          msg.content.trim()
        ) {
          (row.shellOutputChunks ??= []).push(msg.content.trim());
        }
        const compact = metadata.compact_tool_result;
        if (
          compact &&
          typeof compact === "object" &&
          (!isShellLifecycle || (stepType === "tool_end" && !row.shellOutputChunks?.length))
        ) {
          row.compactToolResult = compact as CompactToolResult;
        }
      }
    }
  });

  const sorted = Array.from(rows.values());
  sorted.sort((a, b) => {
    if (a.manifestIndex !== undefined && b.manifestIndex !== undefined) {
      return a.manifestIndex - b.manifestIndex;
    }
    if (a.manifestIndex !== undefined) return -1;
    if (b.manifestIndex !== undefined) return 1;
    return a.firstSeenIndex - b.firstSeenIndex;
  });
  return sorted;
}

function deriveBatchAggregate(messages: ChatMessage[], rows: BatchRowState[]): BatchAggregate {
  let strategy: string | undefined;
  let endStatus: string | undefined;

  for (const msg of messages) {
    const stepType = getStepType(msg);
    const metadata = msg.metadata as Record<string, unknown> | undefined;
    if (stepType === "tool_batch_start") {
      strategy =
        readString(metadata, "effective_execution_strategy") ??
        readString(metadata, "execution_strategy") ??
        strategy;
    } else if (stepType === "tool_batch_end") {
      endStatus = readString(metadata, "status") ?? endStatus;
      strategy = readString(metadata, "execution_strategy") ?? strategy;
    }
  }

  if (endStatus) {
    const normalized = endStatus.toLowerCase();
    if (normalized === "completed") return { status: "completed", strategy };
    if (normalized === "completed_with_errors") return { status: "completed_with_errors", strategy };
    if (normalized === "cancelled") return { status: "cancelled", strategy };
    if (normalized === "denied") return { status: "denied", strategy };
    if (normalized === "failed") return { status: "failed", strategy };
  }

  if (rows.length === 0) return { status: "executing", strategy };
  const allTerminal = rows.every((r) => r.status !== "executing");
  if (!allTerminal) return { status: "executing", strategy };
  const anyFailed = rows.some((r) => r.status === "failed" || r.status === "timed_out");
  const anyCancelled = rows.some((r) => r.status === "cancelled" || r.status === "terminated");
  const anyCompleted = rows.some((r) => r.status === "completed");
  if (anyCancelled && !anyCompleted && !anyFailed) {
    return { status: "cancelled", strategy };
  }
  return { status: anyFailed || anyCancelled ? "completed_with_errors" : "completed", strategy };
}

function aggregateLabel(status: BatchAggregate["status"]): { text: string; color: string } {
  switch (status) {
    case "completed":
      return { text: "Completed", color: "text-emerald-400" };
    case "completed_with_errors":
      return { text: "Completed with errors", color: "text-amber-400" };
    case "failed":
      return { text: "Failed", color: "text-rose-400" };
    case "cancelled":
      return { text: "Cancelled", color: "text-slate-400" };
    case "denied":
      return { text: "Denied", color: "text-slate-400" };
    case "executing":
      return { text: "Running", color: "text-slate-400" };
    default:
      return { text: "", color: "text-slate-400" };
  }
}

export function ToolBatchCard({ messages, groupKey, taskId }: ToolBatchCardProps) {
  const manifest = useMemo(() => buildManifestIndex(messages), [messages]);
  const rows = useMemo(() => buildRows(messages, manifest), [messages, manifest]);
  const aggregate = useMemo(() => deriveBatchAggregate(messages, rows), [messages, rows]);

  if (rows.length === 0) return null;

  // Single-call batch: render through the standalone ExecutingToolCard branch.
  if (rows.length === 1) {
    const only = rows[0];
    return (
      <ExecutingToolCard
        toolName={only.toolName}
        status={only.status}
        sessionStatus={only.sessionStatus}
        processStatus={only.processStatus}
        interactionBoundary={only.interactionBoundary}
        taskId={typeof taskId === "number" || typeof taskId === "string" ? taskId : undefined}
        toolCallId={only.toolCallId}
        stateKey={`${groupKey}-${only.toolCallId}`}
        testId={`tool-batch-card-${groupKey}-row-${only.toolCallId}`}
        retryAttempt={only.retryAttempt}
        retryMaxAttempts={only.retryMaxAttempts}
        compactToolResult={only.compactToolResult}
        commandDisplay={only.commandDisplay}
        transientOutput={only.shellOutputChunks?.join("\n")}
      />
    );
  }

  const completedCount = rows.filter((r) => r.status !== "executing").length;
  const aggregateInfo = aggregateLabel(aggregate.status);

  return (
    <div
      data-testid={`tool-batch-card-${groupKey}`}
      className="mb-1 mr-auto block w-full min-w-0 max-w-[calc(100%-2rem)] overflow-hidden rounded-lg border border-slate-800/60 bg-slate-950/40"
    >
      <div className="flex min-w-0 items-center gap-2 border-b border-slate-800/60 px-3 py-1.5 text-[11px] text-slate-400">
        {aggregate.strategy && (
          <span className="min-w-0 truncate text-slate-500">{aggregate.strategy}</span>
        )}
        <span className="ml-auto shrink-0 whitespace-nowrap text-slate-500">
          {completedCount}/{rows.length}
        </span>
        {aggregateInfo.text && (
          <span className={`ml-2 shrink-0 whitespace-nowrap ${aggregateInfo.color}`}>{aggregateInfo.text}</span>
        )}
      </div>
      <div className="flex w-full min-w-0 flex-col gap-1 px-2 py-1">
        {rows.map((row) => (
          <ExecutingToolCard
            key={row.toolCallId}
            toolName={row.toolName}
            status={row.status}
            sessionStatus={row.sessionStatus}
            processStatus={row.processStatus}
            interactionBoundary={row.interactionBoundary}
            taskId={typeof taskId === "number" || typeof taskId === "string" ? taskId : undefined}
            toolCallId={row.toolCallId}
            stateKey={`${groupKey}-${row.toolCallId}`}
            testId={`tool-batch-card-${groupKey}-row-${row.toolCallId}`}
            retryAttempt={row.retryAttempt}
            retryMaxAttempts={row.retryMaxAttempts}
            compactToolResult={row.compactToolResult}
            commandDisplay={row.commandDisplay}
            transientOutput={row.shellOutputChunks?.join("\n")}
            layout="batch-row"
          />
        ))}
      </div>
    </div>
  );
}

export default ToolBatchCard;
