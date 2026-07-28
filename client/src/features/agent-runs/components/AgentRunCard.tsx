/**
 * Compact subagent-run card for the main chat transcript.
 *
 * Responsibilities:
 * - look and behave like a lightweight reasoning step in the parent transcript
 * - open the parent turn's subagent list without selecting a child thread
 * - present identity and status without visual noise
 */
import { ActivityStatusIcon } from "@/components/chat/ActivityStatusIcon";

import type { AgentRunRecord } from "../state/agent-stream-store";
import { PathfinderIcon } from "./PathfinderIcon";

interface AgentRunCardProps {
  run: AgentRunRecord;
  onOpen: (run: AgentRunRecord) => void;
}

function formatStatus(status: AgentRunRecord["status"]): string {
  switch (status) {
    case "queued":
      return "queued";
    case "running":
      return "working";
    case "waiting_for_approval":
      return "waiting for approval";
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "cancelled":
      return "stopped";
    case "interrupted":
      return "interrupted";
    default:
      return "unknown";
  }
}

function buildAssignmentLine(run: AgentRunRecord): string {
  const assignment = run.assignment;
  if (!assignment) {
    return "Subagent assignment";
  }
  const objective = assignment.objective.trim();
  if (objective) {
    return objective;
  }
  return assignment.scope_summary?.trim() || "Subagent assignment";
}

export function AgentRunCard({ run, onOpen }: AgentRunCardProps) {
  const assignmentLine = buildAssignmentLine(run);
  const isWorking = run.status === "running";

  return (
    <div
      className="mb-1 mr-auto block w-full min-w-0 max-w-[calc(100%-2rem)]"
      data-testid={`agent-run-card-${run.agentRunId}`}
    >
      <button
        type="button"
        onClick={() => onOpen(run)}
        className="inline-flex max-w-full items-center gap-2 rounded-md px-1 py-0.5 text-left text-xs text-slate-400 transition-colors hover:text-slate-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-slate-600"
        aria-label={`Open subagents for ${assignmentLine}`}
      >
        <span className="inline-flex items-center gap-1.5 rounded-md bg-slate-900/70 px-2 py-1">
          <ActivityStatusIcon
            isInProgress={isWorking}
            icon={PathfinderIcon}
            className="h-3.5 w-3.5 shrink-0 text-slate-500"
          />
          <span className="shrink-0 text-slate-300">{run.agentDisplayName}</span>
        </span>
        <span className="shrink-0 text-slate-500">
          {formatStatus(run.status)}
        </span>
      </button>
    </div>
  );
}

export default AgentRunCard;
