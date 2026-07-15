/**
 * Read-only deployment network overview for Management and tenant Runners.
 */
import type { ReactNode } from "react";
import { Cable, CircleDot, ExternalLink, Network, Router, Server, Waypoints } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useNetworkOverview, type RunnerNetworkOverview } from "@/hooks/use-network-overview";

interface NetworkValueProps {
  label: string;
  value: string;
  detail: string;
  icon: ReactNode;
  mono?: boolean;
}

const DEPLOYMENT_PROFILE_LABELS: Readonly<Record<string, string>> = {
  dev_local: "Local development",
  single_host: "Standalone",
  distributed: "Distributed",
};

function NetworkValue({ label, value, detail, icon, mono = false }: NetworkValueProps) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h4 className="text-sm font-medium text-slate-300">{label}</h4>
        <span className="text-slate-500" aria-hidden="true">
          {icon}
        </span>
      </div>
      <p className={`break-words text-base font-semibold text-slate-100 ${mono ? "font-mono" : ""}`}>
        {value}
      </p>
      <p className="mt-1 text-xs leading-5 text-slate-500">{detail}</p>
    </div>
  );
}

function RunnerRow({ runner }: { runner: RunnerNetworkOverview }) {
  const connected = runner.connection_status === "connected";
  const siteDetail = runner.site_network_label
    ? `${runner.site_name} · ${runner.site_network_label}`
    : runner.site_name;

  return (
    <div className="flex flex-col gap-4 rounded-lg border border-slate-800 bg-slate-950/55 p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full ${connected ? "bg-slate-300" : "bg-slate-600"}`}
            aria-hidden="true"
          />
          <h4 className="truncate text-sm font-medium text-slate-200">{runner.name}</h4>
          <span className="rounded border border-slate-700 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-500">
            {connected ? "Connected" : runner.status}
          </span>
        </div>
        <p className="mt-1 pl-3.5 text-xs text-slate-500">{siteDetail}</p>
      </div>
      <div className="sm:text-right">
        <p className="font-mono text-sm font-medium text-slate-200">
          {runner.observed_ip ?? "Not observed yet"}
        </p>
        <p className="mt-1 text-xs text-slate-500">IP observed by Management</p>
      </div>
    </div>
  );
}

export function NetworkSettingsPanel() {
  const overviewQuery = useNetworkOverview();
  const overview = overviewQuery.data;
  const management = overview?.management;
  const profileLabel = overview
    ? (DEPLOYMENT_PROFILE_LABELS[overview.deployment_profile] ?? overview.deployment_profile)
    : "Loading";
  const dnsValue = management?.dns_servers.length
    ? management.dns_servers.join(", ")
    : "Not detected";
  const gatewayDetail = management?.gateway_interface
    ? `Default route via ${management.gateway_interface}`
    : "Default route";
  const refreshStatus = overviewQuery.isError
    ? "Network data unavailable"
    : overviewQuery.isFetching
      ? "Updating network data"
      : "Live network data";

  return (
    <Card className="border-slate-800 bg-slate-900/80 shadow-none">
      <CardHeader className="space-y-1">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle className="text-lg font-semibold text-slate-100">Network overview</CardTitle>
            <CardDescription className="mt-1 text-slate-500">
              Observed addresses and routes for the active deployment
            </CardDescription>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-500" aria-live="polite">
            <span className="rounded border border-slate-700 px-2 py-1 text-slate-400">{profileLabel}</span>
            <span className="h-1.5 w-1.5 rounded-full bg-slate-500" aria-hidden="true" />
            {refreshStatus}
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <section aria-labelledby="management-network-heading">
          <div className="mb-3 flex items-center gap-2">
            <Server className="h-4 w-4 text-slate-500" aria-hidden="true" />
            <h3 id="management-network-heading" className="text-sm font-medium text-slate-300">
              Management network
            </h3>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
            <NetworkValue
              label="Management IP"
              value={management?.primary_ip ?? "Not detected"}
              detail="Primary local interface selected by the default route"
              icon={<Network className="h-4 w-4" />}
              mono
            />
            <NetworkValue
              label="Advertised endpoint"
              value={management?.advertised_host ?? "Not configured"}
              detail={management?.advertised_url ?? "No canonical Management URL available"}
              icon={<ExternalLink className="h-4 w-4" />}
            />
            <NetworkValue
              label="Gateway"
              value={management?.gateway_ip ?? "Not detected"}
              detail={gatewayDetail}
              icon={<Router className="h-4 w-4" />}
              mono
            />
            <NetworkValue
              label="DNS resolvers"
              value={dnsValue}
              detail="Resolvers visible to the Management process"
              icon={<Waypoints className="h-4 w-4" />}
              mono
            />
          </div>

          {management?.interfaces.length ? (
            <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/35 px-4 py-3">
              <div className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-400">
                <Cable className="h-3.5 w-3.5" aria-hidden="true" />
                Active interfaces
              </div>
              <div className="flex flex-wrap gap-2">
                {management.interfaces.map((item) => (
                  <span
                    key={`${item.interface_name}-${item.address}`}
                    className="rounded border border-slate-800 bg-slate-900/70 px-2 py-1 font-mono text-xs text-slate-400"
                  >
                    {item.interface_name} · {item.address}
                    {item.prefix_length == null ? "" : `/${item.prefix_length}`}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
        </section>

        <section aria-labelledby="runner-network-heading">
          <div className="mb-3 flex items-center gap-2">
            <CircleDot className="h-4 w-4 text-slate-500" aria-hidden="true" />
            <div>
              <h3 id="runner-network-heading" className="text-sm font-medium text-slate-300">
                Runner connectivity
              </h3>
              <p className="mt-0.5 text-xs text-slate-500">
                Peer addresses observed on Runner control-channel connections
              </p>
            </div>
          </div>
          <div className="space-y-2">
            {overview?.runners.length ? (
              overview.runners.map((runner) => <RunnerRow key={runner.id} runner={runner} />)
            ) : (
              <div className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
                {overview ? "No Runners registered for this tenant" : "Loading Runner connectivity"}
              </div>
            )}
          </div>
        </section>
      </CardContent>
    </Card>
  );
}
