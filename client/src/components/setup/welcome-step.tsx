/**
 * Welcome screen for the standalone setup wizard.
 */
import { Button } from '@/components/ui/button';
import { Shield, Database, Brain, Settings, CheckCircle, Rocket, Server } from 'lucide-react';

import { SetupActions, SetupCallout, SetupStepHeader } from "@/components/setup/setup-ui";

interface WelcomeStepProps {
  deploymentProfile?: string;
  onNext: () => void;
  onSkip: () => void;
  skipLoading: boolean;
}

export function WelcomeStep({ deploymentProfile, onNext, onSkip, skipLoading }: WelcomeStepProps) {
  const setupAreas = [
    {
      icon: Database,
      title: "Database",
      description: "PostgreSQL credentials and generated deployment config.",
    },
    {
      icon: Shield,
      title: "Admin access",
      description: "Primary admin account and platform secret generation.",
    },
    {
      icon: Brain,
      title: "LLM provider",
      description: "Optional OpenAI key and default model selection.",
    },
    {
      icon: Settings,
      title: "Preferences",
      description: "Timezone and first admin display defaults.",
    },
    {
      icon: Server,
      title: "Runner",
      description: "Default Runner Site for task runtime readiness.",
    },
  ];

  return (
    <div className="space-y-6">
      <SetupStepHeader
        icon={Shield}
        title="Configure DrowAI"
        description={`First-run configuration for this Management install${deploymentProfile ? ` (${deploymentProfile})` : ""}.`}
      />

      <div className="grid gap-3 sm:grid-cols-2">
        {setupAreas.map((area) => (
          <div key={area.title} className="rounded-md border border-slate-800 bg-slate-950/35 p-4">
            <div className="flex items-start gap-3">
              <area.icon className="mt-0.5 h-4 w-4 text-slate-400" />
              <div>
                <h3 className="text-sm font-medium text-slate-100">{area.title}</h3>
                <p className="mt-1 text-sm leading-5 text-slate-400">{area.description}</p>
              </div>
            </div>
          </div>
        ))}
      </div>

      <SetupCallout>
        <div className="flex items-start gap-3">
          <CheckCircle className="mt-0.5 h-4 w-4 text-slate-400" />
          <div>
            <h4 className="font-medium text-slate-100">The wizard will apply durable setup state.</h4>
            <ul className="mt-2 list-disc space-y-1 pl-4 text-slate-400">
              <li>Generate secure configuration files</li>
              <li>Create the PostgreSQL admin account</li>
              <li>Provision the default Runner Site</li>
              <li>Validate the installation before redirecting to login</li>
            </ul>
          </div>
        </div>
      </SetupCallout>

      <SetupActions>
        <Button
          variant="outline"
          onClick={onSkip}
          disabled={skipLoading}
          className="border-slate-700 bg-slate-950 text-slate-200 hover:bg-slate-800 hover:text-white"
        >
          <Rocket className="h-4 w-4" />
          <span>{skipLoading ? 'Setting up...' : 'Quick Start (Default Settings)'}</span>
        </Button>
        
        <Button onClick={onNext}>
          <span>Start Configuration</span>
        </Button>
      </SetupActions>
    </div>
  );
}
