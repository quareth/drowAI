/* Engagement workspace shell with shared tab bar and composed content region. */

import type { ReactNode } from "react";
import { RefreshCw } from "lucide-react";

import { engagementShellPanelClass } from "@/components/engagements/engagement-ui";
import { Button } from "@/components/ui/button";
import {
  WorkspaceTabBar,
  type WorkspaceTabBarItem,
} from "@/components/workspace/workspace-tab-bar";

export interface EngagementWorkspaceTab extends WorkspaceTabBarItem {}

interface EngagementWorkspaceShellProps {
  tabs: readonly EngagementWorkspaceTab[];
  activeTab: string;
  onTabChange: (tabId: string) => void;
  onRefresh: () => void;
  refreshDisabled?: boolean;
  isRefreshing?: boolean;
  children: ReactNode;
}

export function EngagementWorkspaceShell({
  tabs,
  activeTab,
  onTabChange,
  onRefresh,
  refreshDisabled = false,
  isRefreshing = false,
  children,
}: EngagementWorkspaceShellProps) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-gradient-to-b from-slate-950 via-slate-950 to-slate-900/80">
      <WorkspaceTabBar
        tabs={tabs}
        activeTab={activeTab}
        onTabChange={onTabChange}
        actions={
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0 text-slate-300 hover:text-white"
            onClick={onRefresh}
            disabled={refreshDisabled || isRefreshing}
            aria-label={isRefreshing ? "Refreshing engagement workspace" : "Refresh engagement workspace"}
            title={isRefreshing ? "Refreshing..." : "Refresh"}
          >
            <RefreshCw className={isRefreshing ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
          </Button>
        }
        align="start"
        tabListClassName="flex-1 justify-center"
      />

      <div className="flex-1 min-h-0 overflow-hidden p-3 md:p-4">
        <div className={`${engagementShellPanelClass} h-full overflow-hidden`}>{children}</div>
      </div>
    </div>
  );
}
