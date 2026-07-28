/**
 * Drawer list view for subagent runs in one parent conversation.
 *
 * Responsibilities:
 * - group visible subagent runs into active and done sections
 * - keep row activation explicit before showing detail or activity
 * - preserve list scroll offset when users navigate back from detail
 */
import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";

import type { AgentRunRecord } from "../state/agent-stream-store";
import { AgentIdentityIcon } from "./AgentIdentityIcon";

interface AgentRunListProps {
  runs: AgentRunRecord[];
  selectedAgentRunId: string | null;
  initialScrollTop: number;
  onScrollTopChange: (scrollTop: number) => void;
  onSelectRun: (agentRunId: string) => void;
}

const TERMINAL_STATUSES = new Set<AgentRunRecord["status"]>([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);

function isActiveRun(run: AgentRunRecord): boolean {
  return !TERMINAL_STATUSES.has(run.status);
}

function formatRelativeTime(timestamp: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function previewFor(run: AgentRunRecord): string {
  if (run.safeError) return run.safeError;
  if (run.status === "waiting_for_approval") return "Waiting for approval";
  if (run.status === "interrupted") return "Run is no longer active in this backend process.";
  if (run.status === "running") return "Working";
  if (run.status === "queued") return "Queued";
  return run.assignment?.objective?.trim() || "Subagent assignment";
}

function RunRow({
  run,
  selected,
  onSelectRun,
}: {
  run: AgentRunRecord;
  selected: boolean;
  onSelectRun: (agentRunId: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelectRun(run.agentRunId)}
      className={cn(
        "w-full rounded-md px-1 py-2 text-left transition-colors hover:bg-slate-900/60",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-slate-600",
        selected && "bg-slate-900/70",
      )}
      aria-label={`Open ${run.agentDisplayName} thread`}
      data-testid={`agent-run-row-${run.agentRunId}`}
    >
      <span className="flex min-w-0 items-start gap-2.5">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center">
          <AgentIdentityIcon
            agentId={run.agentId}
            displayName={run.agentDisplayName}
            iconKey={run.agentIconKey}
            className="h-5 w-5"
            aria-hidden="true"
          />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="min-w-0 flex-1 truncate text-sm text-slate-200">
              {run.agentDisplayName}
            </span>
            <span className="shrink-0 text-[11px] text-slate-600">
              {formatRelativeTime(run.updatedAt)}
            </span>
          </span>
          <span className="mt-0.5 block truncate text-xs text-slate-500">
            {previewFor(run)}
          </span>
        </span>
      </span>
    </button>
  );
}

function RunSection({
  title,
  runs,
  emptyLabel,
  selectedAgentRunId,
  onSelectRun,
}: {
  title: string;
  runs: AgentRunRecord[];
  emptyLabel: string;
  selectedAgentRunId: string | null;
  onSelectRun: (agentRunId: string) => void;
}) {
  return (
    <section aria-label={`${title} subagent runs`}>
      <h3 className="mb-1.5 text-xs text-slate-500">
        {title} · {runs.length}
      </h3>
      {runs.length === 0 ? (
        <p className="py-1 text-xs text-slate-600">{emptyLabel}</p>
      ) : (
        <div>
          {runs.map((run) => (
            <RunRow
              key={run.agentRunId}
              run={run}
              selected={run.agentRunId === selectedAgentRunId}
              onSelectRun={onSelectRun}
            />
          ))}
        </div>
      )}
    </section>
  );
}

export function AgentRunList({
  runs,
  selectedAgentRunId,
  initialScrollTop,
  onScrollTopChange,
  onSelectRun,
}: AgentRunListProps) {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const activeRuns = runs.filter(isActiveRun);
  const doneRuns = runs.filter((run) => !isActiveRun(run));

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    scroller.scrollTop = initialScrollTop;
  }, [initialScrollTop]);

  if (runs.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6 text-center text-sm text-slate-400">
        No subagent runs are available for this turn.
      </div>
    );
  }

  return (
    <div
      ref={scrollerRef}
      className="min-h-0 flex-1 overflow-y-auto px-4 py-4"
      onScroll={(event) => onScrollTopChange(event.currentTarget.scrollTop)}
      data-testid="agent-run-list"
    >
      <div className="space-y-5">
        <RunSection
          title="Active"
          runs={activeRuns}
          emptyLabel="No active subagents"
          selectedAgentRunId={selectedAgentRunId}
          onSelectRun={onSelectRun}
        />
        <RunSection
          title="Done"
          runs={doneRuns}
          emptyLabel="No completed subagents"
          selectedAgentRunId={selectedAgentRunId}
          onSelectRun={onSelectRun}
        />
      </div>
    </div>
  );
}

export default AgentRunList;
