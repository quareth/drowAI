/**
 * Closed-by-default subagent-run panel contained by the conversation surface.
 *
 * Responsibilities:
 * - bind drawer navigation to explicit presentation-state actions
 * - render beside the parent transcript instead of over it
 * - close back to chat without mutating streamed subagent lifecycle/activity data
 */
import { useCallback, useMemo, useRef } from "react";
import { ArrowLeft, X } from "lucide-react";

import type { ChatMessage } from "@/components/chat/types";
import { Button } from "@/components/ui/button";

import {
  useAgentRunPresentation,
  useAgentRunsForConversation,
  useSelectedAgentRun,
} from "../hooks/use-agent-run";
import {
  type AgentRunRecord,
} from "../state/agent-stream-store";
import {
  closeAgentRunDrawer,
  openAgentRunDetail,
  returnAgentRunDrawerToList,
} from "../state/agent-run-presentation-store";
import { AgentRunDetail } from "./AgentRunDetail";
import { AgentIdentityIcon } from "./AgentIdentityIcon";
import { AgentRunList } from "./AgentRunList";

interface AgentRunDrawerProps {
  taskId: number | null | undefined;
  activityMessages: ChatMessage[];
  onStopRun: (run: AgentRunRecord) => Promise<void> | void;
}

function isValidTaskId(taskId: number | null | undefined): taskId is number {
  return typeof taskId === "number" && Number.isFinite(taskId) && taskId > 0;
}

export function AgentRunDrawer({
  taskId,
  activityMessages,
  onStopRun,
}: AgentRunDrawerProps) {
  const presentation = useAgentRunPresentation(taskId);
  const runs = useAgentRunsForConversation(taskId, presentation.parentRunId);
  const selectedRun = useSelectedAgentRun(taskId);
  const selectedActivityMessages = useMemo(
    () =>
      selectedRun
        ? activityMessages.filter(
            (message) => message.metadata?.agent_run_id === selectedRun.agentRunId,
          )
        : [],
    [activityMessages, selectedRun],
  );
  const listScrollTopRef = useRef(0);
  const resolvedTaskId = isValidTaskId(taskId) ? taskId : null;

  const handleClose = useCallback(() => {
    if (resolvedTaskId) {
      closeAgentRunDrawer(resolvedTaskId);
    }
  }, [resolvedTaskId]);

  const handleSelectRun = useCallback(
    (agentRunId: string) => {
      if (!resolvedTaskId || !presentation.parentRunId) return;
      openAgentRunDetail(resolvedTaskId, presentation.parentRunId, agentRunId);
    },
    [presentation.parentRunId, resolvedTaskId],
  );

  const handleBack = useCallback(() => {
    if (!resolvedTaskId) return;
    returnAgentRunDrawerToList(resolvedTaskId);
  }, [resolvedTaskId]);

  if (!presentation.isOpen || resolvedTaskId === null) {
    return null;
  }

  return (
    <aside
      aria-label="Subagents"
      className="flex h-full min-h-0 w-full flex-col bg-slate-950 text-slate-100"
      data-testid="agent-run-drawer"
    >
      <header className="flex h-10 shrink-0 items-center gap-2 border-b border-slate-800 px-3">
        {presentation.view === "detail" && selectedRun ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={handleBack}
            aria-label="Back to subagent list"
            className="h-7 w-7 text-slate-400 hover:bg-slate-900 hover:text-slate-100"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        ) : null}
        {presentation.view === "detail" && selectedRun ? (
          <AgentIdentityIcon
            agentId={selectedRun.agentId}
            displayName={selectedRun.agentDisplayName}
            iconKey={selectedRun.agentIconKey}
            className="h-4 w-4"
            aria-hidden="true"
          />
        ) : null}
        <span className="min-w-0 flex-1 truncate text-xs font-semibold text-slate-200">
          {presentation.view === "detail" && selectedRun
            ? selectedRun.agentDisplayName
            : "Subagents"}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={handleClose}
          aria-label="Close subagents"
          className="h-7 w-7 text-slate-400 hover:bg-slate-900 hover:text-slate-100"
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </Button>
      </header>

      {presentation.view === "detail" && selectedRun ? (
        <AgentRunDetail
          taskId={resolvedTaskId}
          run={selectedRun}
          activityMessages={selectedActivityMessages}
          onStop={onStopRun}
        />
      ) : (
        <AgentRunList
          runs={runs}
          selectedAgentRunId={presentation.selectedAgentRunId}
          initialScrollTop={listScrollTopRef.current}
          onScrollTopChange={(scrollTop) => {
            listScrollTopRef.current = scrollTop;
          }}
          onSelectRun={handleSelectRun}
        />
      )}
    </aside>
  );
}

export default AgentRunDrawer;
