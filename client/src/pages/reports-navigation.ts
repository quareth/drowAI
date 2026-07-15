/**
 * Reports workspace tab metadata and search destinations.
 */
import { FileText, Library } from "lucide-react";

import { APP_ROUTE_PATHS, buildReportsTabPath } from "@/navigation/routes";
import type { SearchDestination, SearchDestinationProvider } from "@/navigation/types";
import type { WorkspaceTabBarItem } from "@/components/workspace/workspace-tab-bar";

export type ReportsTabId = "library" | "engagement";

export interface ReportsTabDefinition extends WorkspaceTabBarItem {
  id: ReportsTabId;
  description: string;
  keywords: readonly string[];
}

export const REPORTS_TABS: readonly ReportsTabDefinition[] = [
  {
    id: "library",
    label: "Library",
    icon: Library,
    description: "Generated report library",
    keywords: ["reports", "library", "generated reports", "history"],
  },
  {
    id: "engagement",
    label: "Engagement Report",
    icon: FileText,
    description: "Prepare and generate engagement reports",
    keywords: ["report", "engagement", "generate", "memo", "findings"],
  },
];

export const DEFAULT_REPORTS_TAB: ReportsTabId = "library";

export const reportsDestinationProvider: SearchDestinationProvider = {
  id: "reports",
  getDestinations: (): readonly SearchDestination[] => [
    {
      id: "reports",
      label: "Reports",
      description: "Report generation and library",
      group: "Navigation",
      href: APP_ROUTE_PATHS.reports,
      keywords: ["reports", "reporting", "library"],
      icon: FileText,
    },
    ...REPORTS_TABS.map((tab) => ({
      id: `reports.tab.${tab.id}`,
      label: tab.label,
      description: tab.description,
      group: "Reports" as const,
      href: buildReportsTabPath(tab.id),
      keywords: tab.keywords,
      icon: tab.icon,
    })),
  ],
};
