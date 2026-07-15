/**
 * Runner site setup step for the standalone setup wizard.
 */
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, CheckCircle, RefreshCw, Server } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { fetchSetupStatus } from "@/components/setup/setup-api";
import { SetupActions, SetupStepHeader } from "@/components/setup/setup-ui";
import type { SetupRunnerConfig, SetupStatus } from "@/components/setup/setup-types";

interface RunnerStepProps {
  config: SetupRunnerConfig;
  onUpdate: (data: Partial<SetupRunnerConfig>) => void;
  onNext: () => void;
  onPrevious: () => void;
}

export function RunnerStep({ config, onUpdate, onNext, onPrevious }: RunnerStepProps) {
  const { data: setupStatus, refetch, isFetching } = useQuery<SetupStatus>({
    queryKey: ["/api/setup/status"],
    queryFn: fetchSetupStatus,
    refetchInterval: (query) => (query.state.data?.runner_connected ? false : 5000),
  });
  const runnerConnected = Boolean(setupStatus?.runner_connected);

  return (
    <div className="space-y-6">
      <SetupStepHeader
        icon={Server}
        title="Runner"
        description="Configure the default Runner Site used for task runtime execution."
      />

      <div className="space-y-4">
        <div>
          <Label htmlFor="site_name">Runner Site Name</Label>
          <Input
            id="site_name"
            value={config.site_name}
            onChange={(event) => onUpdate({ site_name: event.target.value })}
          />
        </div>
      </div>

      <Alert
        className={
          runnerConnected
            ? "border-emerald-800/70 bg-emerald-950/25 text-emerald-100"
            : "border-amber-800/70 bg-amber-950/25 text-amber-100"
        }
      >
        {runnerConnected ? (
          <>
            <CheckCircle className="h-4 w-4 text-emerald-300" />
            <AlertDescription>
              Runtime readiness is available. A Runner is connected to this Runner Site.
            </AlertDescription>
          </>
        ) : (
          <>
            <AlertCircle className="h-4 w-4 text-amber-300" />
            <AlertDescription>
              Runtime readiness is waiting for a Runner connection. Local Runner enrollment is published
              when setup completes; remote Runner Site packages are installed after sign-in.
            </AlertDescription>
          </>
        )}
      </Alert>

      <SetupActions>
        <Button variant="outline" onClick={onPrevious} className="border-slate-700 bg-slate-950 text-slate-200 hover:bg-slate-800 hover:text-white">
          Previous
        </Button>
        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={() => refetch()}
            disabled={isFetching}
            className="border-slate-700 bg-slate-950 text-slate-200 hover:bg-slate-800 hover:text-white"
          >
            {isFetching ? <RefreshCw className="h-4 w-4 animate-spin" /> : "Refresh status"}
          </Button>
          <Button onClick={onNext}>Next</Button>
        </div>
      </SetupActions>
    </div>
  );
}
