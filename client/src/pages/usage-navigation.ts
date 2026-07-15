/**
 * Usage page navbar-search destination provider.
 */
import { Gauge } from "lucide-react";

import { APP_ROUTE_PATHS } from "@/navigation/routes";
import type { SearchDestinationProvider } from "@/navigation/types";

export const usageDestinationProvider: SearchDestinationProvider = {
  id: "usage",
  getDestinations: () => [
    {
      id: "usage",
      label: "Usage",
      description: "Token and cost usage insights",
      group: "Navigation" as const,
      href: APP_ROUTE_PATHS.usage,
      keywords: ["usage", "tokens", "cost", "billing", "insights"],
      icon: Gauge,
    },
  ],
};
