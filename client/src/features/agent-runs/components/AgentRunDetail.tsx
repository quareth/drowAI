/**
 * Drawer detail view for one subagent run.
 *
 * Responsibilities:
 * - render one subagent run as a compact child conversation
 * - expose Stop and pending-approval status without owning HITL controls
 * - reuse existing reasoning, tool, observation, and final-message renderers
 */
import { useState } from "react";
import { AlertTriangle, CircleStop } from "lucide-react";

import type { ChatMessage } from "@/components/chat/types";
import { Button } from "@/components/ui/button";
import { MarkdownMessage } from "@/components/ui/markdown-message";

import { isAgentRunTerminalStatus } from "../contracts/agent-run";
import type { AgentRunRecord } from "../state/agent-stream-store";
import { AgentActivityTimeline } from "./AgentActivityTimeline";

interface AgentRunDetailProps {
  taskId: number;
  run: AgentRunRecord;
  activityMessages: ChatMessage[];
  canStopRuns: boolean;
  onStop: (run: AgentRunRecord) => Promise<void> | void;
}

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

export function AgentRunDetail({
  taskId,
  run,
  activityMessages,
  canStopRuns,
  onStop,
}: AgentRunDetailProps) {
  const [isStopping, setIsStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const canStop = canStopRuns && !isAgentRunTerminalStatus(run.status) && !isStopping;

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

          {run.status === "waiting_for_approval" && (
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
            {run.result && (
              <article
                className="rounded-lg bg-slate-900/50 px-3 py-2 text-slate-200"
                aria-label={`${run.agentDisplayName} final message`}
                data-testid="agent-run-final-message"
              >
                <MarkdownMessage content={run.result.summary} />
              </article>
            )}
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
