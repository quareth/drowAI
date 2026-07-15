/**
 * Settings section metadata and navbar-search destinations.
 */
import type { ComponentType } from "react";
import { Database, Globe, Network, Server, Settings, Shield, Trash2 } from "lucide-react";

import { APP_ROUTE_PATHS, buildSettingsSectionPath } from "@/navigation/routes";
import type { SearchDestination, SearchDestinationProvider } from "@/navigation/types";

export type SettingsSectionId =
  | "api"
  | "network"
  | "runner-sites"
  | "system"
  | "data-management"
  | "display"
  | "cve";

export interface SettingsSectionDefinition {
  id: SettingsSectionId;
  label: string;
  description: string;
  keywords: readonly string[];
  icon: ComponentType<{ className?: string }>;
}

export const SETTINGS_SECTIONS: readonly SettingsSectionDefinition[] = [
  {
    id: "api",
    label: "API",
    description: "Provider, model, and API key settings",
    keywords: ["api", "provider", "model", "openai", "key", "llm"],
    icon: Shield,
  },
  {
    id: "network",
    label: "Network",
    description: "Network and runtime connectivity settings",
    keywords: ["network", "docker", "vpn", "isolation", "connectivity"],
    icon: Network,
  },
  {
    id: "runner-sites",
    label: "Runner Sites",
    description: "Runner enrollment and site status",
    keywords: ["runner", "site", "enrollment", "management", "docker"],
    icon: Server,
  },
  {
    id: "system",
    label: "System",
    description: "System storage and runtime preferences",
    keywords: ["system", "storage", "runtime", "cleanup"],
    icon: Database,
  },
  {
    id: "data-management",
    label: "Data Management",
    description: "Retention and deletion policy settings",
    keywords: ["data", "retention", "delete", "cleanup", "privacy"],
    icon: Trash2,
  },
  {
    id: "display",
    label: "Display",
    description: "Language, timezone, and display preferences",
    keywords: ["display", "language", "timezone", "time zone", "locale"],
    icon: Globe,
  },
  {
    id: "cve",
    label: "CVE",
    description: "CVE enrichment and vulnerability settings",
    keywords: ["cve", "vulnerability", "nvd", "security"],
    icon: Settings,
  },
];

export const DEFAULT_SETTINGS_SECTION: SettingsSectionId = "api";

export const settingsDestinationProvider: SearchDestinationProvider = {
  id: "settings",
  getDestinations: (): readonly SearchDestination[] => [
    {
      id: "settings",
      label: "Settings",
      description: "Application preferences and configuration",
      group: "Navigation",
      href: APP_ROUTE_PATHS.settings,
      keywords: ["settings", "preferences", "configuration"],
      icon: Settings,
    },
    ...SETTINGS_SECTIONS.map((section) => ({
      id: `settings.section.${section.id}`,
      label: `${section.label} Settings`,
      description: section.description,
      group: "Settings" as const,
      href: buildSettingsSectionPath(section.id),
      keywords: section.keywords,
      icon: section.icon,
    })),
  ],
};
