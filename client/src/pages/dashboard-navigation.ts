/**
 * Dashboard workspace tab metadata and search destinations.
 */
import { FileSearch, Flag, Folder, Shield } from "lucide-react";

import { APP_ROUTE_PATHS, buildDashboardWorkspacePath } from "@/navigation/routes";
import type { SearchDestination, SearchDestinationProvider } from "@/navigation/types";
import type { WorkspaceTabBarItem } from "@/components/workspace/workspace-tab-bar";

export type DashboardWorkspaceId = "overview" | "files" | "threats";

export interface DashboardWorkspaceDefinition extends WorkspaceTabBarItem {
  id: DashboardWorkspaceId;
  description: string;
  keywords: readonly string[];
}

export const DASHBOARD_WORKSPACES: readonly DashboardWorkspaceDefinition[] = [
  {
    id: "overview",
    label: "Operations",
    icon: Flag,
    description: "Task operations and command post",
    keywords: ["outpost", "tasks", "operations", "command post", "dashboard"],
  },
  {
    id: "files",
    label: "File Explorer",
    icon: Folder,
    description: "Browse task workspace files",
    keywords: ["files", "workspace", "downloads", "artifacts"],
  },
  {
    id: "threats",
    label: "Threat Dashboard",
    icon: Shield,
    description: "Analyst threat overview",
    keywords: ["threats", "risk", "posture", "findings"],
  },
];

export const dashboardDestinationProvider: SearchDestinationProvider = {
  id: "dashboard",
  getDestinations: (): readonly SearchDestination[] => [
    {
      id: "dashboard.home",
      label: "Outpost",
      description: "Main pentest workspace",
      group: "Navigation",
      href: APP_ROUTE_PATHS.dashboard,
      keywords: ["home", "dashboard", "operations", "tasks"],
      icon: FileSearch,
    },
    ...DASHBOARD_WORKSPACES.map((workspace) => ({
      id: `dashboard.workspace.${workspace.id}`,
      label: workspace.label,
      description: workspace.description,
      group: "Workspace" as const,
      href: buildDashboardWorkspacePath(workspace.id),
      keywords: workspace.keywords,
      icon: workspace.icon,
    })),
  ],
};
