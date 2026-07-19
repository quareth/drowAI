/**
 * Minimal deployment selector for backend-provided LLM deployment metadata.
 *
 * This component renders only the supplied deployment reference; it does not
 * discover arbitrary deployments or expose provider marketplace controls.
 */
import { CheckCircle } from "lucide-react";

import type { LLMDeploymentRef } from "@/features/llm-provider/types";
import { Button } from "@/components/ui/button";

export interface DeploymentPickerProps {
  deploymentRef: LLMDeploymentRef | null | undefined;
  runnable: boolean | undefined;
  lifecycleState?: string | null;
  onSelect: () => void;
  isPending?: boolean;
}

export function DeploymentPicker({
  deploymentRef,
  runnable,
  lifecycleState,
  onSelect,
  isPending = false,
}: DeploymentPickerProps) {
  const canSelect = Boolean(deploymentRef) && runnable === true && lifecycleState === "enabled";

  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0 text-xs text-slate-300">
        <div className="flex items-center gap-2">
          <CheckCircle className="h-3.5 w-3.5 text-emerald-400" />
          <span>Deployment: {deploymentRef ? "available" : "pending"}</span>
        </div>
        <p className="mt-1 text-slate-500">
          Runtime: {runnable ? "runnable" : "not runnable"}
        </p>
      </div>
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={!canSelect || isPending}
        onClick={onSelect}
        className="border-slate-600 text-slate-200 hover:text-white"
      >
        Select deployment
      </Button>
    </div>
  );
}

export default DeploymentPicker;
