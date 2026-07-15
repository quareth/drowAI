/**
 * Final review and completion step for the standalone setup wizard.
 */
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertCircle,
  CheckCircle,
  Clock,
  Database,
  Loader2,
  LogIn,
  Monitor,
  Server,
  Shield,
} from "lucide-react";

import { ReviewRow, SetupActions, SetupCallout, SetupStepHeader } from "@/components/setup/setup-ui";
import type { SetupCompleteResponse, SetupConfig } from "@/components/setup/setup-types";

interface CompleteStepProps {
  config: SetupConfig;
  onComplete: () => void;
  onPrevious: () => void;
  isLoading: boolean;
  error: Error | null;
  result: SetupCompleteResponse | null;
  onSignIn: () => void;
}

export function CompleteStep({
  config,
  onComplete,
  onPrevious,
  isLoading,
  error,
  result,
  onSignIn,
}: CompleteStepProps) {
  if (result) {
    const runnerReady = result.runner_readiness === "ready";

    return (
      <div className="space-y-6">
        <SetupStepHeader
          icon={CheckCircle}
          title="Setup complete"
          description="DrowAI is ready for sign-in. Runtime readiness depends on a connected Runner."
        />

        <div className="grid gap-3 sm:grid-cols-2">
          <CompletionStatusCard
            title="Setup"
            description="Control-plane setup is complete."
            state="complete"
          />
          <CompletionStatusCard
            title="Runner enrollment"
            description={
              result.runner_enrollment_published
                ? "Local Runner enrollment was published for this Runner Site."
                : "Install a Runner Site package from Settings after sign-in."
            }
            state={result.runner_enrollment_published ? "complete" : "waiting"}
          />
          <CompletionStatusCard
            title="Runtime readiness"
            description={
              runnerReady
                ? "A Runner is connected and ready for task runtime work."
                : "Waiting for a Runner connection before task runtime work can start."
            }
            state={runnerReady ? "complete" : "waiting"}
          />
          <CompletionStatusCard
            title="Next step"
            description={
              runnerReady
                ? "Sign in and create tasks through the connected Runner."
                : "Sign in, then confirm the Runner connection or install a Runner Site package."
            }
            state="next"
          />
        </div>

        {result.restart_required ? (
          <Alert className="border-amber-800/70 bg-amber-950/25 text-amber-100">
            <AlertCircle className="h-4 w-4 text-amber-300" />
            <AlertDescription>
              Setup completed, but runtime services need attention before Runner readiness can update.
            </AlertDescription>
          </Alert>
        ) : null}

        <SetupActions>
          <div />
          <Button onClick={onSignIn} className="flex items-center space-x-2">
            <LogIn className="h-4 w-4" />
            <span>Sign in</span>
          </Button>
        </SetupActions>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SetupStepHeader
        icon={CheckCircle}
        title="Review and complete"
        description="Review the selected configuration before provisioning the standalone installation."
      />

      <div className="grid gap-3">
        <ReviewRow icon={Database} title="Database">
          <p>
            Database: <code className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-200">{config.database.db_name}</code>
          </p>
          <p>
            User: <code className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-200">{config.database.db_user}</code>
          </p>
        </ReviewRow>

        <ReviewRow icon={Shield} title="Security">
          <p>
            Admin: <code className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-200">{config.security.admin_username}</code>
          </p>
          <p>Session timeout: {config.security.session_timeout} minutes</p>
          <p>JWT and encryption keys: generated automatically</p>
        </ReviewRow>

        <ReviewRow icon={Monitor} title="Display">
          <p>Timezone: {config.display.timezone}</p>
        </ReviewRow>

        <ReviewRow icon={Server} title="Runner">
          <p>Runner Site: {config.runner.site_name}</p>
          <p>Runtime readiness requires a connected Runner.</p>
        </ReviewRow>
      </div>

      <SetupCallout>
        <h4 className="font-medium text-slate-100">What happens next</h4>
        <ul className="mt-2 list-disc space-y-1 pl-4 text-slate-400">
          <li>Update generated deployment config and rotate the database password</li>
          <li>Create the admin account in PostgreSQL</li>
          <li>Store display defaults</li>
          <li>Provision the default Runner Site and local Runner enrollment artifact</li>
          <li>Show runtime readiness and the sign-in action</li>
        </ul>
      </SetupCallout>

      {error ? (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>Setup failed: {error.message}</AlertDescription>
        </Alert>
      ) : null}

      <SetupActions>
        <Button
          variant="outline"
          onClick={onPrevious}
          disabled={isLoading}
          className="border-slate-700 bg-slate-950 text-slate-200 hover:bg-slate-800 hover:text-white"
        >
          Previous
        </Button>
        <Button onClick={onComplete} disabled={isLoading} className="flex items-center space-x-2">
          {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
          <span>{isLoading ? "Completing setup..." : "Complete Installation"}</span>
        </Button>
      </SetupActions>
    </div>
  );
}

interface CompletionStatusCardProps {
  title: string;
  description: string;
  state: "complete" | "waiting" | "next";
}

function CompletionStatusCard({ title, description, state }: CompletionStatusCardProps) {
  const Icon = state === "waiting" ? Clock : CheckCircle;
  const className =
    state === "waiting"
      ? "border-amber-800/70 bg-amber-950/25 text-amber-100"
      : state === "next"
        ? "border-blue-800/70 bg-blue-950/25 text-blue-100"
        : "border-emerald-800/70 bg-emerald-950/25 text-emerald-100";
  const iconClassName =
    state === "waiting" ? "text-amber-300" : state === "next" ? "text-blue-300" : "text-emerald-300";

  return (
    <div className={`flex gap-3 rounded-md border p-4 ${className}`}>
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-current/30 bg-slate-950/20">
        <Icon className={`h-4 w-4 ${iconClassName}`} />
      </div>
      <div className="min-w-0">
        <h3 className="text-sm font-medium">{title}</h3>
        <p className="mt-1 text-sm leading-5 opacity-85">{description}</p>
      </div>
    </div>
  );
}
