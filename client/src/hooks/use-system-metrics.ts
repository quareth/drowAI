/**
 * Polling query contract for authenticated management-host resource metrics.
 */
import { useQuery } from "@tanstack/react-query";

import { SETTINGS_OVERVIEW_POLL_INTERVAL_MS } from "@/config/settings-overview";

export const SYSTEM_METRICS_QUERY_KEY = ["/api/settings/system/metrics"] as const;
export const SYSTEM_METRICS_POLL_INTERVAL_MS = SETTINGS_OVERVIEW_POLL_INTERVAL_MS;

export interface ResourceUsage {
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  usage_percent: number;
}

export interface SystemMetrics {
  memory: ResourceUsage;
  storage: ResourceUsage;
  uptime_seconds: number;
  collected_at: string;
}

export function useSystemMetrics() {
  return useQuery<SystemMetrics>({
    queryKey: SYSTEM_METRICS_QUERY_KEY,
    refetchInterval: SYSTEM_METRICS_POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}
