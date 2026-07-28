/**
 * Drawer detail view for one subagent run.
 *
 * Responsibilities:
 * - render one subagent run as a compact child conversation
 * - expose Stop and pending approval controls without changing chat behavior
 * - reuse existing reasoning, tool, observation, and final-message renderers
 */
import { useState } from "react";
import { AlertTriangle, CircleStop, ShieldQuestion } from "lucide-react";

import ToolApprovalCard, { type BatchApprovalDecisions } from "@/components/chat/ToolApprovalCard";
import type { ChatMessage } from "@/components/chat/types";
import { Button } from "@/components/ui/button";
import type { ToolApprovalInterruptDetail } from "@/types/hitl";

import type { AgentRunRecord } from "../state/agent-stream-store";
import { AgentActivityTimeline } from "./AgentActivityTimeline";

export interface AgentRunApprovalControls {
  interrupt: ToolApprovalInterruptDetail | null;
  onApprove: () => void;
  onEdit: (params: Record<string, unknown>) => void;
  onSkip: () => void;
  onBatchSubmit?: (decisions: BatchApprovalDecisions) => void;
  isSubmitting?: boolean;
}

type VisibleAgentRunApprovalControls = AgentRunApprovalControls & {
  interrupt: ToolApprovalInterruptDetail;
};

interface AgentRunDetailProps {
  taskId: number;
  run: AgentRunRecord;
  activityMessages: ChatMessage[];
  approvalControls?: AgentRunApprovalControls;
  onStop: (run: AgentRunRecord) => Promise<void> | void;
}

const ACTIVE_STATUSES = new Set<AgentRunRecord["status"]>([
  "queued",
  "running",
  "waiting_for_approval",
]);

function formatDuration(run: AgentRunRecord): string {
  const end = run.completedAt ?? run.updatedAt;
  const seconds = Math.max(0, Math.floor((end - run.createdAt) / 1000));
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) {
    return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function shouldShowApproval(
  run: AgentRunRecord,
  controls: AgentRunApprovalControls | undefined,
): controls is VisibleAgentRunApprovalControls {
  return (
    run.status === "waiting_for_approval" &&
    controls?.interrupt != null &&
    controls.interrupt.taskId === run.taskId &&
    controls.interrupt.graphName === "scout_recon"
  );
}

export function AgentRunDetail({
  taskId,
  run,
  activityMessages,
  approvalControls,
  onStop,
}: AgentRunDetailProps) {
  const [isStopping, setIsStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const canStop = ACTIVE_STATUSES.has(run.status) && !isStopping;
  const visibleApprovalControls = shouldShowApproval(run, approvalControls)
    ? approvalControls
    : null;

  const handleStop = async () => {
    if (!canStop) return;
    setIsStopping(true);
    setStopError(null);
    try {
      await onStop(run);
    } catch (error) {
      setStopError(error instanceof Error ? error.message : "Failed to stop subagent run.");
    } finally {
      setIsStopping(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="agent-run-detail">
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <div className="space-y-3">
          <div className="border-b border-slate-800 pb-2 text-xs text-slate-500">
            Worked for {formatDuration(run)}
          </div>

          {run.safeError && (
            <section className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-3 text-sm text-rose-100">
              <span className="inline-flex items-center gap-2 font-medium">
                <AlertTriangle className="h-4 w-4" aria-hidden="true" />
                {run.safeError}
              </span>
            </section>
          )}

          {run.status === "interrupted" && (
            <section className="rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-3 text-sm text-slate-300">
              This subagent run was replayed, but the current backend process no longer owns it.
            </section>
          )}

          {visibleApprovalControls && (
            <section className="space-y-2">
              <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-amber-200">
                <ShieldQuestion className="h-3.5 w-3.5" aria-hidden="true" />
                Approval
              </h3>
              <ToolApprovalCard
                payload={visibleApprovalControls.interrupt.payload}
                onApprove={visibleApprovalControls.onApprove}
                onEdit={visibleApprovalControls.onEdit}
                onSkip={visibleApprovalControls.onSkip}
                onBatchSubmit={visibleApprovalControls.onBatchSubmit}
                isSubmitting={visibleApprovalControls.isSubmitting}
              />
            </section>
          )}

          {run.status === "waiting_for_approval" && !visibleApprovalControls && (
            <section className="rounded-lg border border-amber-500/35 bg-amber-500/10 px-3 py-3 text-sm text-amber-100">
              {run.agentDisplayName} is waiting for a tool approval.
            </section>
          )}

          <section
            className="space-y-2"
            aria-label={`${run.agentDisplayName} conversation`}
          >
            <AgentActivityTimeline
              taskId={taskId}
              messages={activityMessages}
              agentDisplayName={run.agentDisplayName}
            />
          </section>

          {canStop && (
          <section>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={handleStop}
              className="px-2 text-slate-500 hover:bg-slate-900 hover:text-slate-300"
            >
              <CircleStop className="h-4 w-4" aria-hidden="true" />
              {isStopping ? "Stopping" : "Stop"}
            </Button>
          </section>
          )}

          {stopError && (
            <p className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
              {stopError}
            </p>
          )}

        </div>
      </div>
    </div>
  );
}

export default AgentRunDetail;
