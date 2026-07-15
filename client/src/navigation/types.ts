/**
 * Shared contracts for app-wide destination search and deep-link navigation.
 */
import type { ComponentType } from "react";

export type SearchDestinationGroup =
  | "Navigation"
  | "Workspace"
  | "Knowledge"
  | "Settings"
  | "Reports"
  | "Profile";

export interface SearchDestinationContext {
  permissions: ReadonlySet<string>;
}

export interface SearchDestination {
  id: string;
  label: string;
  description?: string;
  group: SearchDestinationGroup;
  href: string;
  keywords: readonly string[];
  icon?: ComponentType<{ className?: string }>;
  isVisible?: (context: SearchDestinationContext) => boolean;
}

export interface SearchDestinationProvider {
  id: string;
  getDestinations: (context: SearchDestinationContext) => readonly SearchDestination[];
}

export interface SearchMatch {
  destination: SearchDestination;
  score: number;
}

export interface SearchResultGroup {
  group: SearchDestinationGroup;
  matches: SearchMatch[];
}
