/**
 * Knowledge workspace tab metadata and search destinations.
 */
import type { ComponentType } from "react";
import { Archive, FileSearch, Globe, Map as MapIcon, ScrollText } from "lucide-react";

import { APP_ROUTE_PATHS, buildKnowledgeTabPath } from "@/navigation/routes";
import type { SearchDestination, SearchDestinationProvider } from "@/navigation/types";

export type KnowledgeWorkspaceTabId = "summary" | "findings" | "assets" | "evidence" | "map";

export interface KnowledgeWorkspaceTabDefinition {
  id: KnowledgeWorkspaceTabId;
  label: string;
  description: string;
  keywords: readonly string[];
  icon: ComponentType<{ className?: string }>;
}

export const KNOWLEDGE_WORKSPACE_TABS: readonly KnowledgeWorkspaceTabDefinition[] = [
  {
    id: "summary",
    label: "Briefing",
    description: "Knowledge overview and durable security summary",
    keywords: ["knowledge", "summary", "briefing", "overview"],
    icon: ScrollText,
  },
  {
    id: "findings",
    label: "Findings",
    description: "Canonical findings and vulnerabilities",
    keywords: ["findings", "finding", "vulnerability", "risk", "severity"],
    icon: FileSearch,
  },
  {
    id: "assets",
    label: "Assets",
    description: "Hosts, IP addresses, services, and assets",
    keywords: ["asset", "assets", "ip", "host", "hostname", "service"],
    icon: Globe,
  },
  {
    id: "evidence",
    label: "Evidence",
    description: "Archived evidence and provenance",
    keywords: ["evidence", "proof", "artifact", "provenance"],
    icon: Archive,
  },
  {
    id: "map",
    label: "Territory",
    description: "Network and asset relationship map",
    keywords: ["territory", "map", "network", "cidr", "relationship"],
    icon: MapIcon,
  },
];

export const DEFAULT_KNOWLEDGE_TAB: KnowledgeWorkspaceTabId = "summary";

export const knowledgeDestinationProvider: SearchDestinationProvider = {
  id: "knowledge",
  getDestinations: (): readonly SearchDestination[] => [
    {
      id: "knowledge",
      label: "Knowledge",
      description: "Security knowledge workspace",
      group: "Navigation",
      href: APP_ROUTE_PATHS.knowledge,
      keywords: ["knowledge", "assets", "findings", "evidence"],
      icon: ScrollText,
    },
    ...KNOWLEDGE_WORKSPACE_TABS.map((tab) => ({
      id: `knowledge.tab.${tab.id}`,
      label: tab.label,
      description: tab.description,
      group: "Knowledge" as const,
      href: buildKnowledgeTabPath(tab.id),
      keywords: tab.keywords,
      icon: tab.icon,
    })),
  ],
};
