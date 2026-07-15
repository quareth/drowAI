/** Polling query contract for the read-only deployment network overview. */

import { useQuery } from "@tanstack/react-query";

import { SETTINGS_OVERVIEW_POLL_INTERVAL_MS } from "@/config/settings-overview";

export const NETWORK_OVERVIEW_QUERY_KEY = ["/api/settings/network/overview"] as const;

export interface NetworkInterfaceAddress {
  interface_name: string;
  address: string;
  family: "ipv4" | "ipv6";
  prefix_length: number | null;
  is_loopback: boolean;
}

export interface ManagementNetworkOverview {
  advertised_url: string | null;
  advertised_host: string | null;
  advertised_url_source: string;
  primary_ip: string | null;
  interfaces: NetworkInterfaceAddress[];
  gateway_ip: string | null;
  gateway_interface: string | null;
  dns_servers: string[];
}

export interface RunnerNetworkOverview {
  id: string;
  name: string;
  site_id: string;
  site_name: string;
  site_network_label: string | null;
  status: string;
  connection_status: string;
  observed_ip: string | null;
  observed_at: string | null;
}

export interface NetworkOverview {
  deployment_profile: string;
  management: ManagementNetworkOverview;
  runners: RunnerNetworkOverview[];
  collected_at: string;
}

export function useNetworkOverview() {
  return useQuery<NetworkOverview>({
    queryKey: NETWORK_OVERVIEW_QUERY_KEY,
    refetchInterval: SETTINGS_OVERVIEW_POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}
