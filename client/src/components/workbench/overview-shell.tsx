/**
 * Purpose: Compose the Overview workbench with a global terminal dock.
 */
import { TaskPanel } from "@/components/panels/task-panel";
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
  const { effectivePermissions } = useTenantContext();
  const canControlTask = hasTenantAction(
    toTenantActionSet(effectivePermissions),
    TENANT_ACTIONS.taskControl,
  );

  const toggleTerminalCollapse = () => {
    toggleTerminalCollapsed();
  };
  const mainOverviewContent = (
    <ResizablePanelGroup direction="horizontal" className="h-full">
      <ResizablePanel
        defaultSize={40}
        minSize={30}
        order={1}
      >
        <TaskPanel />
      </ResizablePanel>
      <ResizableHandle
        className="w-0.5 bg-slate-800/30 hover:bg-emerald-500/30 transition-colors"
      />
      <ResizablePanel
        defaultSize={60}
        minSize={40}
        order={2}
      >
        <UnifiedAgentChat taskId={null} chatMode={chatMode} onChatModeChange={onChatModeChange} />
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
