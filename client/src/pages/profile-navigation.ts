/**
 * Profile page tab metadata and search destinations.
 */
import type { ComponentType } from "react";
import { KeyRound, Shield, UserRound } from "lucide-react";

import { APP_ROUTE_PATHS, buildProfileTabPath } from "@/navigation/routes";
import type { SearchDestination, SearchDestinationProvider } from "@/navigation/types";

export type ProfileTabId = "overview" | "access" | "security";

export interface ProfileTabDefinition {
  id: ProfileTabId;
  label: string;
  description: string;
  keywords: readonly string[];
  icon: ComponentType<{ className?: string }>;
}

export const PROFILE_TABS: readonly ProfileTabDefinition[] = [
  {
    id: "overview",
    label: "Overview",
    description: "Account identity and profile details",
    keywords: ["profile", "account", "user", "overview"],
    icon: UserRound,
  },
  {
    id: "access",
    label: "Access",
    description: "Tenant access and effective permissions",
    keywords: ["access", "tenant", "role", "permissions"],
    icon: Shield,
  },
  {
    id: "security",
    label: "Security",
    description: "Password and account security",
    keywords: ["security", "password", "account"],
    icon: KeyRound,
  },
];

export const DEFAULT_PROFILE_TAB: ProfileTabId = "overview";

export const profileDestinationProvider: SearchDestinationProvider = {
  id: "profile",
  getDestinations: (): readonly SearchDestination[] => [
    {
      id: "profile",
      label: "Profile",
      description: "Account identity and access",
      group: "Navigation",
      href: APP_ROUTE_PATHS.profile,
      keywords: ["profile", "account", "user"],
      icon: UserRound,
    },
    ...PROFILE_TABS.map((tab) => ({
      id: `profile.tab.${tab.id}`,
      label: `Profile ${tab.label}`,
      description: tab.description,
      group: "Profile" as const,
      href: buildProfileTabPath(tab.id),
      keywords: tab.keywords,
      icon: tab.icon,
    })),
  ],
};
