/**
 * Purpose: Compose the Overview workbench with a global terminal dock.
 */
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { ImperativePanelHandle } from "react-resizable-panels";

import { TaskPanel } from "@/components/panels/task-panel";
import { TaskPanelVisibilityButton } from "@/components/panels/task-panel-visibility-button";
import { TerminalPanel } from "@/components/panels/terminal-panel";
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from "@/components/ui/resizable";
import { UnifiedAgentChat } from "@/components/chat/UnifiedAgentChat";
import type { ChatExperienceMode } from "@/components/chat/types";
import { useTenantContext } from "@/hooks/use-tenant-context";
import { TENANT_ACTIONS, hasTenantAction, toTenantActionSet } from "@/lib/tenant-permissions";
import { toggleTerminalCollapsed, useWorkbenchStateSnapshot } from "@/state/workbench-state-store";

interface OverviewShellProps {
  chatMode: ChatExperienceMode;
  onChatModeChange: (mode: ChatExperienceMode) => void;
}

export function OverviewShell({ chatMode, onChatModeChange }: OverviewShellProps) {
  const { isTerminalCollapsed } = useWorkbenchStateSnapshot();
  const taskPanelRef = useRef<ImperativePanelHandle>(null);
  const previousTerminalCollapsedRef = useRef(isTerminalCollapsed);
  const [isTaskPanelCollapsed, setIsTaskPanelCollapsed] = useState(false);
  const { effectivePermissions } = useTenantContext();
  const canControlTask = hasTenantAction(
    toTenantActionSet(effectivePermissions),
    TENANT_ACTIONS.taskControl,
  );

  const toggleTerminalCollapse = () => {
    toggleTerminalCollapsed();
  };

  const toggleTaskPanelVisibility = useCallback(() => {
    const panel = taskPanelRef.current;
    if (!panel) {
      return;
    }
    if (panel.isCollapsed()) {
      panel.expand();
      return;
    }
    panel.collapse();
  }, []);

  useLayoutEffect(() => {
    const terminalLayoutChanged =
      previousTerminalCollapsedRef.current !== isTerminalCollapsed;
    previousTerminalCollapsedRef.current = isTerminalCollapsed;
    const panel = taskPanelRef.current;
    if (
      terminalLayoutChanged
      && isTaskPanelCollapsed
      && panel
      && !panel.isCollapsed()
    ) {
      panel.collapse();
    }
  }, [isTaskPanelCollapsed, isTerminalCollapsed]);

  const mainOverviewContent = (
    <ResizablePanelGroup direction="horizontal" className="h-full">
      <ResizablePanel
        ref={taskPanelRef}
        id="task-panel"
        defaultSize={40}
        minSize={30}
        maxSize={40}
        collapsible
        collapsedSize={0}
        onCollapse={() => setIsTaskPanelCollapsed(true)}
        onExpand={() => setIsTaskPanelCollapsed(false)}
        order={1}
        className="min-w-0 overflow-hidden"
      >
        {isTaskPanelCollapsed ? null : (
          <TaskPanel onToggleVisibility={toggleTaskPanelVisibility} />
        )}
      </ResizablePanel>
      <ResizableHandle
        aria-label="Resize task panel"
        className={`w-0.5 bg-slate-800/30 transition-colors hover:bg-emerald-500/30 ${
          isTaskPanelCollapsed ? "hidden" : ""
        }`}
      />
      <ResizablePanel
        id="conversation-panel"
        defaultSize={60}
        minSize={60}
        order={2}
      >
        <UnifiedAgentChat
          taskId={null}
          chatMode={chatMode}
          onChatModeChange={onChatModeChange}
          leadingHeaderSlot={
            isTaskPanelCollapsed ? (
              <TaskPanelVisibilityButton
                isCollapsed
                onToggle={toggleTaskPanelVisibility}
              />
            ) : undefined
          }
        />
      </ResizablePanel>
    </ResizablePanelGroup>
  );

  if (!canControlTask) {
    return <div className="h-full min-h-0 flex flex-col">{mainOverviewContent}</div>;
  }

  return (
    <div className="h-full min-h-0 flex flex-col">
      {isTerminalCollapsed ? (
        <>
          <div className="flex-1 min-h-0">{mainOverviewContent}</div>
          <TerminalPanel isCollapsed onToggleCollapse={toggleTerminalCollapse} />
        </>
      ) : (
        <ResizablePanelGroup direction="vertical" className="h-full min-h-0">
          <ResizablePanel defaultSize={68} minSize={30}>
            {mainOverviewContent}
          </ResizablePanel>
          <ResizableHandle className="h-0.5 bg-slate-800/30 hover:bg-emerald-500/30 transition-colors" />
          <ResizablePanel defaultSize={32} minSize={20}>
            <TerminalPanel isCollapsed={false} onToggleCollapse={toggleTerminalCollapse} />
          </ResizablePanel>
        </ResizablePanelGroup>
      )}
    </div>
  );
}
