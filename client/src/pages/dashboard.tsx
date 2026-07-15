/* Main operator dashboard with Operations, File Explorer, and Threat Dashboard workspaces. */

import { useEffect, useState } from "react";
import { useLocation } from "wouter";

import { Navbar } from "@/components/layout/navbar";
import { Sidebar } from "@/components/layout/sidebar";
import {
  FileExplorerPanel,
  type FileExplorerSelection,
} from "@/components/panels/file-explorer-panel";
import { FilePreviewPanel } from "@/components/panels/file-preview-panel";
import { ThreatDashboardPanel } from "@/components/panels/threat-dashboard-panel";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { WorkspaceTabBar } from "@/components/workspace/workspace-tab-bar";
import { OverviewShell } from "@/components/workbench/overview-shell";
import type { ChatExperienceMode } from "@/components/chat/types";
import { buildDashboardWorkspacePath, readAllowedQueryValue, ROUTE_QUERY_KEYS } from "@/navigation/routes";
import {
  DASHBOARD_WORKSPACES,
  type DashboardWorkspaceId,
} from "@/pages/dashboard-navigation";

const DASHBOARD_WORKSPACE_IDS = DASHBOARD_WORKSPACES.map((workspace) => workspace.id);

export default function Dashboard() {
  const [location, setLocation] = useLocation();
  const locationWorkspace = readAllowedQueryValue(
    location,
    ROUTE_QUERY_KEYS.dashboardWorkspace,
    DASHBOARD_WORKSPACE_IDS,
    "overview",
  );
  const [activeTab, setActiveTab] = useState<DashboardWorkspaceId>(locationWorkspace);
  const [chatMode, setChatMode] = useState<ChatExperienceMode>("agent");
  const [fileExplorerSelection, setFileExplorerSelection] = useState<FileExplorerSelection>({
    taskId: null,
    filePath: null,
  });

  useEffect(() => {
    setActiveTab(locationWorkspace);
  }, [locationWorkspace]);

  const handleTabChange = (tabId: string) => {
    const nextTab = tabId as DashboardWorkspaceId;
    setActiveTab(nextTab);
    setLocation(buildDashboardWorkspacePath(nextTab));
  };

  const renderTabContent = () => {
    switch (activeTab) {
      case "overview":
        return <OverviewShell chatMode={chatMode} onChatModeChange={setChatMode} />;
      case "files":
        return (
          <ResizablePanelGroup direction="horizontal" className="h-full">
            <ResizablePanel defaultSize={30} minSize={25}>
              <FileExplorerPanel
                selectedTaskId={fileExplorerSelection.taskId}
                selectedFile={fileExplorerSelection.filePath}
                onSelectionChange={setFileExplorerSelection}
              />
            </ResizablePanel>
            <ResizableHandle className="w-0.5 bg-slate-800/30 hover:bg-emerald-500/30 transition-colors" />
            <ResizablePanel defaultSize={70} minSize={30}>
              <FilePreviewPanel
                taskId={fileExplorerSelection.taskId}
                filePath={fileExplorerSelection.filePath}
              />
            </ResizablePanel>
          </ResizablePanelGroup>
        );
      case "threats":
        return <ThreatDashboardPanel />;
      default:
        return null;
    }
  };

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      <Navbar />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <div className="flex-1 flex flex-col overflow-hidden">
          <WorkspaceTabBar
            tabs={DASHBOARD_WORKSPACES}
            activeTab={activeTab}
            onTabChange={handleTabChange}
            align="center"
          />
          <div className="flex-1 overflow-hidden">{renderTabContent()}</div>
        </div>
      </div>
    </div>
  );
}
