/**
 * Composes feature-owned destination providers for navbar search.
 */
import { dashboardDestinationProvider } from "@/pages/dashboard-navigation";
import { knowledgeDestinationProvider } from "@/pages/knowledge-workspace-navigation";
import { profileDestinationProvider } from "@/pages/profile-navigation";
import { reportsDestinationProvider } from "@/pages/reports-navigation";
import { settingsDestinationProvider } from "@/components/settings/settings-navigation";
import { usageDestinationProvider } from "@/pages/usage-navigation";
import type {
  SearchDestination,
  SearchDestinationContext,
  SearchDestinationProvider,
} from "@/navigation/types";

const APP_DESTINATION_PROVIDERS: readonly SearchDestinationProvider[] = [
  dashboardDestinationProvider,
  knowledgeDestinationProvider,
  reportsDestinationProvider,
  settingsDestinationProvider,
  profileDestinationProvider,
  usageDestinationProvider,
];

export function getAppSearchDestinations(
  context: SearchDestinationContext,
): SearchDestination[] {
  return APP_DESTINATION_PROVIDERS.flatMap((provider) =>
    provider.getDestinations(context).filter((destination) =>
      destination.isVisible ? destination.isVisible(context) : true,
    ),
  );
}
