/**
 * First-run setup wizard page for standalone DrowAI installations.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Check, AlertCircle, Loader2 } from "lucide-react";

import { WelcomeStep } from "@/components/setup/welcome-step";
import { DatabaseStep } from "@/components/setup/database-step";
import { SecurityStep } from "@/components/setup/security-step";
import { DisplayStep } from "@/components/setup/display-step";
import { RunnerStep } from "@/components/setup/runner-step";
import { CompleteStep } from "@/components/setup/complete-step";
import { completeSetup, fetchSetupStatus, skipSetupWizard } from "@/components/setup/setup-api";
import {
  SETUP_STEPS,
  type SetupCompleteResponse,
  type SetupConfig,
  type SetupStatus,
} from "@/components/setup/setup-types";
import { cn } from "@/lib/utils";

const INITIAL_CONFIG: SetupConfig = {
  database: {
    db_name: "drowai",
    db_user: "drowai_user",
    db_password: "",
  },
  security: {
    session_timeout: 30,
    admin_username: "admin",
    admin_email: "admin@drowai.local",
    admin_password: "",
  },
  display: {
    timezone: "UTC",
  },
  network: {},
  runner: {
    create_site: true,
    site_name: "Default Site",
    site_slug: "default-site",
  },
};

export default function SetupPage() {
  const [, setLocation] = useLocation();
  const [currentStep, setCurrentStep] = useState(1);
  const [config, setConfig] = useState<SetupConfig>(INITIAL_CONFIG);
  const [completionResponse, setCompletionResponse] = useState<SetupCompleteResponse | null>(null);
  const queryClient = useQueryClient();

  const { data: setupStatus, isLoading: statusLoading } = useQuery<SetupStatus>({
    queryKey: ["/api/setup/status"],
    queryFn: fetchSetupStatus,
    refetchOnWindowFocus: false,
  });

  const completeMutation = useMutation({
    mutationFn: completeSetup,
    onSuccess: (response: SetupCompleteResponse) => {
      setCompletionResponse(response);
      queryClient.setQueryData<SetupStatus | undefined>(["/api/setup/status"], (previous) => {
        if (!previous) {
          return previous;
        }
        return {
          ...previous,
          setup_required: false,
          installation_complete: true,
          installation_status: "complete",
          setup_error: null,
          runner_connected: response.runner_readiness === "ready" || previous.runner_connected,
        };
      });
    },
  });

  const skipMutation = useMutation({
    mutationFn: skipSetupWizard,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["/api/setup/status"] });
      setLocation("/auth");
    },
  });

  const handleNext = () => {
    if (currentStep < SETUP_STEPS.length) {
      setCurrentStep(currentStep + 1);
    }
  };

  const handlePrevious = () => {
    if (currentStep > 1) {
      setCurrentStep(currentStep - 1);
    }
  };

  const updateConfig = <K extends keyof SetupConfig>(section: K, data: Partial<SetupConfig[K]>) => {
    setConfig((previous) => ({
      ...previous,
      [section]: { ...previous[section], ...data },
    }));
  };

  useEffect(() => {
    if (statusLoading || !setupStatus) {
      return;
    }
    if (completionResponse) {
      return;
    }
    if (!setupStatus.wizard_enabled || !setupStatus.setup_required) {
      setLocation("/auth");
    }
  }, [completionResponse, setLocation, setupStatus, statusLoading]);

  const handleComplete = () => {
    if (completionResponse || completeMutation.isPending || completeMutation.isSuccess) {
      return;
    }
    completeMutation.mutate(config);
  };

  const handleSignIn = () => {
    setLocation(completionResponse?.redirect || "/auth");
  };

  if (statusLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950">
        <Card className="w-96 border-slate-800 bg-slate-900 text-slate-100">
          <CardContent className="flex items-center justify-center p-8">
            <Loader2 className="h-8 w-8 animate-spin" />
            <span className="ml-2">Checking setup status...</span>
          </CardContent>
        </Card>
      </div>
    );
  }

  const progress = (currentStep / SETUP_STEPS.length) * 100;
  const currentStepMeta = SETUP_STEPS[currentStep - 1];

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col gap-6 px-5 py-8 lg:grid lg:grid-cols-[280px_minmax(0,1fr)] lg:px-8">
        <aside className="rounded-lg border border-slate-800 bg-slate-900/55 p-5 lg:sticky lg:top-8 lg:h-[calc(100vh-4rem)]">
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-[0.18em] text-slate-500">First-run setup</p>
            <h1 className="text-2xl font-semibold text-slate-50">DrowAI</h1>
            <p className="text-sm leading-6 text-slate-400">
              Configure the control plane with the minimum required installation settings.
            </p>
            {setupStatus?.deployment_profile ? (
              <p className="inline-flex rounded-md border border-slate-800 bg-slate-950 px-2.5 py-1 text-xs text-slate-400">
                {setupStatus.deployment_profile}
              </p>
            ) : null}
          </div>

          <div className="mt-7 h-1 overflow-hidden rounded-full bg-slate-800">
            <div className="h-full rounded-full bg-blue-500 transition-all duration-300" style={{ width: `${progress}%` }} />
          </div>

          <nav className="mt-6 space-y-1" aria-label="Setup steps">
            {SETUP_STEPS.map((step) => {
              const isComplete = step.id < currentStep;
              const isCurrent = step.id === currentStep;

              return (
                <div
                  key={step.id}
                  className={cn(
                    "flex items-start gap-3 rounded-md px-3 py-2.5 transition-colors",
                    isCurrent ? "bg-slate-800/80 text-slate-50" : "text-slate-400",
                  )}
                >
                  <div
                    className={cn(
                      "mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md border text-xs font-medium",
                      isComplete
                        ? "border-blue-500/60 bg-blue-500/15 text-blue-300"
                        : isCurrent
                          ? "border-slate-500 bg-slate-900 text-slate-100"
                          : "border-slate-700 bg-slate-900 text-slate-500",
                    )}
                  >
                    {isComplete ? <Check className="h-3.5 w-3.5" /> : step.id}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium">{step.title}</p>
                    <p className="mt-0.5 text-xs text-slate-500">{step.description}</p>
                  </div>
                </div>
              );
            })}
          </nav>
        </aside>

        <main className="min-w-0">
          <div className="mb-4 flex items-center justify-between text-sm text-slate-500">
            <span>
              Step {currentStep} of {SETUP_STEPS.length}
            </span>
            <span>{currentStepMeta?.title}</span>
          </div>

          <Card className="border-slate-800 bg-slate-900/70 shadow-none">
            <CardContent className="p-5 sm:p-7">
              {currentStep === 1 ? (
                <WelcomeStep
                  deploymentProfile={setupStatus?.deployment_profile}
                  onNext={handleNext}
                  onSkip={() => skipMutation.mutate()}
                  skipLoading={skipMutation.isPending}
                />
              ) : null}
              {currentStep === 2 ? (
                <DatabaseStep
                  config={config.database}
                  onUpdate={(data) => updateConfig("database", data)}
                  onNext={handleNext}
                  onPrevious={handlePrevious}
                />
              ) : null}
              {currentStep === 3 ? (
                <SecurityStep
                  config={config.security}
                  onUpdate={(data) => updateConfig("security", data)}
                  onNext={handleNext}
                  onPrevious={handlePrevious}
                />
              ) : null}
              {currentStep === 4 ? (
                <DisplayStep
                  config={config.display}
                  onUpdate={(data) => updateConfig("display", data)}
                  onNext={handleNext}
                  onPrevious={handlePrevious}
                />
              ) : null}
              {currentStep === 5 ? (
                <RunnerStep
                  config={config.runner}
                  onUpdate={(data) => updateConfig("runner", data)}
                  onNext={handleNext}
                  onPrevious={handlePrevious}
                />
              ) : null}
              {currentStep === 6 ? (
                <CompleteStep
                  config={config}
                  onComplete={handleComplete}
                  onPrevious={handlePrevious}
                  isLoading={completeMutation.isPending || completeMutation.isSuccess}
                  error={completeMutation.error as Error | null}
                  result={completionResponse}
                  onSignIn={handleSignIn}
                />
              ) : null}
            </CardContent>
          </Card>

          {(completeMutation.error || skipMutation.error) && currentStep !== SETUP_STEPS.length ? (
            <Alert className="mt-4 border-red-900/60 bg-red-950/50 text-red-100" variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {(completeMutation.error as Error)?.message ||
                  (skipMutation.error as Error)?.message ||
                  "An error occurred during setup"}
              </AlertDescription>
            </Alert>
          ) : null}
        </main>
      </div>
    </div>
  );
}
