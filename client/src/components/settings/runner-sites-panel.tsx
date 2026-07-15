/**
 * Runner Site settings panel for Management-owned runner enrollment.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, RefreshCw, Server, Trash2 } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { apiFetch } from "@/lib/api-config";

interface RunnerSite {
  id: string;
  name: string;
  slug: string;
  status: string;
  connectivity_status: string;
  runner_count: number;
  connected_runner_count: number;
  last_seen_at: string | null;
  network_label: string | null;
  labels: Record<string, string>;
  created_at: string;
  updated_at: string;
}

interface RunnerReadiness {
  status: string;
  ready: boolean;
  reason_codes: string[];
  runner_site_count: number;
  connected_runner_count: number;
  evaluated_runner_count: number;
  selected_runner_id: string | null;
  execution_site_id: string | null;
}

interface ManagementUrlConfig {
  management_url: string;
  source: string;
}

const RUNNER_SITES_QUERY_KEY = ["/api/runner-control/runner-sites"] as const;
const MANAGEMENT_URL_QUERY_KEY = ["/api/runner-control/management-url"] as const;
const DEFAULT_INSTALL_COMMANDS = [
  "tar xzf drowai-runner-site-*.tar.gz",
  "cd drowai-runner-site",
  "docker compose up -d --build",
] as const;

export function RunnerSitesPanel() {
  const queryClient = useQueryClient();
  const [siteName, setSiteName] = useState("Primary Runner Site");
  const [managementUrl, setManagementUrl] = useState(() => window.location.origin);
  const [managementUrlEdited, setManagementUrlEdited] = useState(false);
  const [tlsVerify, setTlsVerify] = useState(() => window.location.protocol === "https:");

  const { data: managementUrlConfig } = useQuery<ManagementUrlConfig>({
    queryKey: MANAGEMENT_URL_QUERY_KEY,
  });

  useEffect(() => {
    const resolvedUrl = managementUrlConfig?.management_url?.trim();
    if (resolvedUrl && !managementUrlEdited) {
      setManagementUrl(resolvedUrl);
      setTlsVerify(resolvedUrl.startsWith("https://"));
    }
  }, [managementUrlConfig, managementUrlEdited]);

  const { data: sites = [], isFetching } = useQuery<RunnerSite[]>({
    queryKey: RUNNER_SITES_QUERY_KEY,
    refetchInterval: 5000,
  });

  const readinessQueries = useQueries({
    queries: sites.map((site) => ({
      queryKey: runnerSiteReadinessQueryKey(site.id),
      refetchInterval: 5000,
    })),
  });
  const readinessBySiteId = useMemo(() => {
    const bySiteId = new Map<string, RunnerReadiness>();
    sites.forEach((site, index) => {
      const readiness = readinessQueries[index]?.data as RunnerReadiness | undefined;
      if (readiness) {
        bySiteId.set(site.id, readiness);
      }
    });
    return bySiteId;
  }, [readinessQueries, sites]);

  const downloadPackage = useMutation({
    mutationFn: async (site?: RunnerSite) => {
      const response = await apiFetch("/api/runner-control/enrollments/package", {
        method: "POST",
        body: JSON.stringify({
          site_name: site?.name ?? siteName,
          site_slug: site?.slug,
          management_url: managementUrl,
          tls_verify: tlsVerify,
          allow_insecure_management_url: managementUrl.startsWith("http://"),
        }),
      });
      if (!response.ok) {
        const message = await response.text().catch(() => `HTTP ${response.status}`);
        throw new Error(`${response.status}: ${message}`);
      }
      return {
        blob: await response.blob(),
        filename: readFilename(response.headers.get("content-disposition")) ?? "drowai-runner-site.tar.gz",
      };
    },
    onSuccess: ({ blob, filename }) => {
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
      void queryClient.invalidateQueries({ queryKey: RUNNER_SITES_QUERY_KEY });
    },
  });

  const removeRunnerSite = useMutation({
    mutationFn: async (site: RunnerSite) => {
      if (!window.confirm(runnerSiteRemovalConfirmation(site))) {
        return false;
      }
      const response = await apiFetch(`/api/runner-control/runner-sites/${site.id}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw await runnerSiteRemovalError(response);
      }
      return true;
    },
    onSuccess: (removed, site) => {
      if (removed) {
        queryClient.setQueryData<RunnerSite[]>(RUNNER_SITES_QUERY_KEY, (currentSites) =>
          currentSites?.filter((currentSite) => currentSite.id !== site.id),
        );
        void queryClient.invalidateQueries({ queryKey: RUNNER_SITES_QUERY_KEY });
      }
    },
  });

  const installCommands = DEFAULT_INSTALL_COMMANDS.join("\n");
  const createNewRunnerSitePackage = () => {
    const name = siteName.trim();
    const confirmed = window.confirm(
      `Create a new Runner Site named "${name}" and download its package? Use an existing Runner Site's Download Package button if this runner belongs to an existing site.`,
    );
    if (confirmed) {
      downloadPackage.mutate(undefined);
    }
  };

  return (
    <Card className="bg-slate-900 border-slate-700">
      <CardHeader>
        <CardTitle className="text-white flex items-center">
          <Server className="w-5 h-5 mr-2" />
          Runner Sites
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <div>
          <Label className="text-gray-300">Management URL</Label>
          <Input
            value={managementUrl}
            onChange={(event) => {
              setManagementUrlEdited(true);
              setManagementUrl(event.target.value);
            }}
            className="bg-slate-800 border-slate-600 text-white mt-1"
          />
          <p className="mt-1 text-xs text-slate-400">
            Runner packages use this address to connect back to Management.
          </p>
        </div>

        <div className="flex items-center justify-between">
          <div>
            <h4 className="text-white font-medium">Verify TLS certificates</h4>
            <p className="text-gray-400 text-sm">Enable this for HTTPS Management URLs with trusted certificates.</p>
          </div>
          <Switch checked={tlsVerify} onCheckedChange={setTlsVerify} />
        </div>

        {downloadPackage.error instanceof Error || removeRunnerSite.error instanceof Error ? (
          <Alert variant="destructive">
            <AlertDescription>
              {downloadPackage.error instanceof Error
                ? downloadPackage.error.message
                : removeRunnerSite.error instanceof Error
                  ? removeRunnerSite.error.message
                  : ""}
            </AlertDescription>
          </Alert>
        ) : null}

        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h4 className="text-white font-medium">Sites</h4>
            {isFetching ? <RefreshCw className="h-4 w-4 animate-spin text-slate-400" /> : null}
          </div>
          {sites.length === 0 ? (
            <p className="text-gray-400 text-sm">No Runner Sites registered yet.</p>
          ) : (
            <div className="space-y-2">
              {sites.map((site) => {
                const rowStatus = runnerSiteRowStatus(site, readinessBySiteId.get(site.id));
                return (
                  <div key={site.id} className="flex flex-col gap-3 rounded border border-slate-700 px-3 py-3 md:flex-row md:items-center md:justify-between">
                    <div className="min-w-0 space-y-2">
                      <div className="text-white font-medium">{site.name}</div>
                      <div className="text-gray-400 text-sm">
                        {site.slug}
                        {site.last_seen_at ? ` · last seen ${new Date(site.last_seen_at).toLocaleString()}` : ""}
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="outline">Registered: {site.runner_count}</Badge>
                        <Badge variant={site.connected_runner_count > 0 ? "default" : "outline"}>
                          Connected: {site.connected_runner_count}
                        </Badge>
                      </div>
                      <p className="text-sm text-slate-300">{rowStatus.description}</p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={rowStatus.variant}>
                        {rowStatus.label}
                      </Badge>
                      <Button
                        size="sm"
                        onClick={() => downloadPackage.mutate(site)}
                        disabled={downloadPackage.isPending || !managementUrl.trim()}
                      >
                        {downloadPackage.isPending ? (
                          <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                        ) : (
                          <Download className="h-4 w-4 mr-2" />
                        )}
                        Download Package
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => removeRunnerSite.mutate(site)}
                        disabled={removeRunnerSite.isPending}
                      >
                        {removeRunnerSite.isPending ? (
                          <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                        ) : (
                          <Trash2 className="h-4 w-4 mr-2" />
                        )}
                        Remove
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <Separator className="bg-slate-700" />

        <div className="space-y-3">
          <h4 className="text-white font-medium">New Runner Site</h4>
          <div>
            <Label className="text-gray-300">Runner Site name</Label>
            <Input
              value={siteName}
              onChange={(event) => setSiteName(event.target.value)}
              className="bg-slate-800 border-slate-600 text-white mt-1"
            />
          </div>
          <Button
            variant="outline"
            onClick={createNewRunnerSitePackage}
            disabled={downloadPackage.isPending || !siteName.trim() || !managementUrl.trim()}
          >
            {downloadPackage.isPending ? (
              <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Download className="h-4 w-4 mr-2" />
            )}
            Create New Runner Site Package
          </Button>
          <pre className="rounded border border-slate-700 bg-slate-950 p-3 text-xs text-slate-100 overflow-x-auto">
            {installCommands}
          </pre>
        </div>
      </CardContent>
    </Card>
  );
}

function readFilename(contentDisposition: string | null): string | null {
  const match = /filename="?([^";]+)"?/i.exec(contentDisposition ?? "");
  return match?.[1] ?? null;
}

function runnerSiteReadinessQueryKey(siteId: string): readonly [string] {
  return [`/api/runner-control/readiness?execution_site_id=${encodeURIComponent(siteId)}`] as const;
}

function runnerSiteRowStatus(
  site: RunnerSite,
  readiness: RunnerReadiness | undefined,
): { label: string; description: string; variant: "default" | "secondary" | "destructive" | "outline" } {
  const connectivityStatus = normalizeStatus(site.connectivity_status);
  const readinessStatus = normalizeStatus(readiness?.status);
  const reasonCodes = new Set(readiness?.reason_codes ?? []);

  if (readiness?.ready && site.connected_runner_count > 0) {
    return {
      label: `Ready ${site.connected_runner_count}/${site.runner_count}`,
      description: "Live connectivity is available and this Runner Site can accept task runtime work.",
      variant: "default",
    };
  }

  if (site.runner_count === 0 || reasonCodes.has("NO_RUNNERS_REGISTERED")) {
    return {
      label: "Waiting for Runner",
      description: "No Runners are registered for this Runner Site.",
      variant: "secondary",
    };
  }

  if (readinessStatus === "runner_capacity_exhausted" || reasonCodes.has("RUNNER_CAPACITY_EXHAUSTED")) {
    return {
      label: "Capacity full",
      description: "Runner capacity is exhausted. Existing Runners are connected but have no task slots available.",
      variant: "secondary",
    };
  }

  if (readinessStatus === "runner_incompatible" || hasAnyReason(reasonCodes, INCOMPATIBLE_REASON_CODES)) {
    return {
      label: "Not compatible",
      description: "A Runner is registered, but its protocol, runtime version, labels, or capabilities do not match task requirements.",
      variant: "destructive",
    };
  }

  if (
    site.connected_runner_count === 0 ||
    connectivityStatus === "offline" ||
    readinessStatus === "runner_registered_offline" ||
    hasAnyReason(reasonCodes, OFFLINE_REASON_CODES)
  ) {
    return {
      label: `Offline ${site.connected_runner_count}/${site.runner_count}`,
      description: "Runners are registered, but no live connection is available for task runtime work.",
      variant: "destructive",
    };
  }

  return {
    label: "Checking readiness",
    description: "Runner connectivity is present; capacity and task readiness are still being evaluated.",
    variant: "secondary",
  };
}

function runnerSiteRemovalConfirmation(site: RunnerSite): string {
  const offlineRunnerCount = Math.max(site.runner_count - site.connected_runner_count, 0);
  const offlineWarning = offlineRunnerCount > 0
    ? "\n\nOffline or unreachable Runner hosts cannot be stopped by Management. Stop those Runner deployments manually after removal."
    : "";
  return `Permanently remove Runner Site "${site.name}" and unregister all of its Runners?${offlineWarning}`;
}

async function runnerSiteRemovalError(response: Response): Promise<Error> {
  const fallbackMessage = `Runner Site removal failed (HTTP ${response.status}).`;
  const payload = await response.json().catch(() => null);
  const detail = isRecord(payload) && isRecord(payload.detail)
    ? payload.detail
    : isRecord(payload)
      ? payload
      : {};
  const code = typeof detail.error_code === "string" ? detail.error_code : "";

  if (code === "RUNNER_SITE_ACTIVE_EXECUTIONS") {
    const rawCount = detail.active_execution_count ?? detail.execution_count;
    const count = typeof rawCount === "number" ? rawCount : null;
    const executionText = count === null
      ? "active executions"
      : `${count} active execution${count === 1 ? "" : "s"}`;
    return new Error(
      `RUNNER_SITE_ACTIVE_EXECUTIONS: This Runner Site has ${executionText}. Stop them before removing the site.`,
    );
  }

  if (code === "RUNNER_SITE_LAST_CONNECTED") {
    return new Error(
      "RUNNER_SITE_LAST_CONNECTED: Connect another Runner Site before removing this one. At least one connected Runner must remain.",
    );
  }

  if (code === "RUNNER_SITE_NOT_FOUND") {
    return new Error("RUNNER_SITE_NOT_FOUND: This Runner Site no longer exists or is not available.");
  }

  const message = typeof detail?.message === "string" && detail.message.trim()
    ? detail.message
    : fallbackMessage;
  return new Error(message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function normalizeStatus(value: string | null | undefined): string {
  return String(value ?? "").trim().toLowerCase();
}

function hasAnyReason(reasonCodes: Set<string>, candidates: readonly string[]): boolean {
  return candidates.some((reasonCode) => reasonCodes.has(reasonCode));
}

const OFFLINE_REASON_CODES = [
  "RUNNER_CREDENTIAL_NOT_ACTIVE",
  "RUNNER_HEARTBEAT_STALE",
  "RUNNER_MAINTENANCE_MODE",
  "RUNNER_NOT_ONLINE",
  "RUNNER_REVOKED",
  "RUNNER_STALE_OR_OFFLINE",
] as const;

const INCOMPATIBLE_REASON_CODES = [
  "RUNNER_CAPABILITY_MISMATCH",
  "RUNNER_EXECUTION_SITE_MISMATCH",
  "RUNNER_LABEL_MISMATCH",
  "RUNNER_PROTOCOL_INCOMPATIBLE",
  "RUNNER_RUNTIME_VERSION_INCOMPATIBLE",
] as const;
