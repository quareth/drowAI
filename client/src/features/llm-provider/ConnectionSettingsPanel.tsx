/**
 * Metadata-driven proving connection controls for one approved LLM preset.
 *
 * The panel renders only backend-declared user configuration fields and never
 * accepts endpoint URLs, headers, or arbitrary provider inventory values.
 */
import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, Key, Loader2, ShieldCheck } from "lucide-react";

import {
  createLLMProvingConnection,
  enableLLMProvingConnection,
  saveLLMDeploymentSelection,
  testLLMProvingConnection,
} from "@/features/llm-provider/api";
import DeploymentPicker from "@/features/llm-provider/DeploymentPicker";
import type {
  LLMCatalogModel,
  LLMConnectionRef,
  LLMDeploymentRef,
  LLMProvingMetadata,
  LLMProvingVerification,
} from "@/features/llm-provider/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface ConnectionSettingsPanelProps {
  model: LLMCatalogModel;
  proving: LLMProvingMetadata;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;
const selectionQueryKey = ["/api/llm/selection"] as const;

export function ConnectionSettingsPanel({
  model,
  proving,
  onSuccess,
  onError,
}: ConnectionSettingsPanelProps) {
  const queryClient = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [connectionRef, setConnectionRef] = useState<LLMConnectionRef | null>(
    proving.connectionRef ?? null,
  );
  const [deploymentRef, setDeploymentRef] = useState<LLMDeploymentRef | null>(
    proving.deploymentRef ?? model.deploymentRef ?? null,
  );
  const [lifecycleState, setLifecycleState] = useState(
    proving.lifecycleState || "unknown",
  );
  const [verification, setVerification] = useState<LLMProvingVerification | null>(
    proving.verification ?? null,
  );
  const [runnable, setRunnable] = useState(
    Boolean(proving.runnability?.runnable ?? model.runnable),
  );

  const requiresApiKey = useMemo(
    () => proving.userConfigFields.includes("api_key"),
    [proving.userConfigFields],
  );
  const canTest = Boolean(apiKey.trim() && connectionRef && deploymentRef);
  const canEnable = verification?.status === "passed" && Boolean(connectionRef && deploymentRef);

  const invalidateCatalog = async () => {
    await queryClient.invalidateQueries({ queryKey: catalogQueryKey });
  };

  const createMutation = useMutation({
    mutationFn: () => createLLMProvingConnection(proving.presetId, {
      api_key: apiKey.trim() || null,
    }),
    onSuccess: async (status) => {
      setConnectionRef(status.connectionRef ?? connectionRef);
      setDeploymentRef(status.deploymentRef ?? deploymentRef);
      setLifecycleState(status.lifecycleState);
      if (status.verification) {
        setVerification(status.verification);
      }
      setRunnable(Boolean(status.runnability?.runnable ?? false));
      await invalidateCatalog();
      onSuccess("Proving draft created", "The proving connection draft is ready.");
    },
    onError: (error) => onError(
      "Proving draft failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const testMutation = useMutation({
    mutationFn: () => testLLMProvingConnection(proving.presetId, {
      api_key: apiKey.trim(),
      connection_ref: connectionRef,
      deployment_ref: deploymentRef,
    }),
    onSuccess: async (result) => {
      setVerification(result);
      await invalidateCatalog();
      onSuccess("Proving connection verified", result.message);
    },
    onError: (error) => onError(
      "Proving connection test failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const enableMutation = useMutation({
    mutationFn: () => enableLLMProvingConnection(proving.presetId, {
      connection_ref: connectionRef as LLMConnectionRef,
      deployment_ref: deploymentRef as LLMDeploymentRef,
    }),
    onSuccess: async (status) => {
      setLifecycleState(status.lifecycleState);
      setConnectionRef(status.connectionRef ?? connectionRef);
      setDeploymentRef(status.deploymentRef ?? deploymentRef);
      setRunnable(Boolean(status.runnability?.runnable ?? runnable));
      await invalidateCatalog();
      onSuccess("Proving connection enabled", "The proving deployment can be selected.");
    },
    onError: (error) => onError(
      "Proving enable failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const selectMutation = useMutation({
    mutationFn: () => saveLLMDeploymentSelection({
      deployment_ref: deploymentRef as LLMDeploymentRef,
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: selectionQueryKey }),
        invalidateCatalog(),
      ]);
      onSuccess("Proving deployment selected", `${model.label} is selected for chat.`);
    },
    onError: (error) => onError(
      "Proving deployment selection failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const busy =
    createMutation.isPending ||
    testMutation.isPending ||
    enableMutation.isPending ||
    selectMutation.isPending;

  return (
    <Card className="border-slate-700 bg-slate-900">
      <CardHeader className="space-y-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <CardTitle className="flex items-center text-base text-white">
              <ShieldCheck className="mr-2 h-4 w-4" />
              {proving.displayName}
            </CardTitle>
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
              <Badge className="bg-slate-700 text-slate-200">
                Lifecycle: {lifecycleState}
              </Badge>
              <Badge className="bg-slate-700 text-slate-200">
                Verification: {verification?.code ?? "not_tested"}
              </Badge>
              <Badge className="bg-slate-700 text-slate-200">
                Runnability: {proving.runnability?.status ?? "unknown"}
              </Badge>
            </div>
          </div>
          <div className="text-xs text-slate-300">
            <p>Context: {model.contextWindowTokens} tokens</p>
            <p>Pricing: {model.pricingStatus ?? "unavailable"}</p>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {requiresApiKey ? (
          <div>
            <Label htmlFor={`llm-proving-key-${proving.presetId}`} className="text-white">
              Proving API Key
            </Label>
            <div className="relative mt-2">
              <Input
                id={`llm-proving-key-${proving.presetId}`}
                type="password"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                autoComplete="off"
                className="border-slate-600 bg-slate-800 pr-10 text-white"
              />
              <Key className="absolute right-3 top-2.5 h-4 w-4 text-slate-500" />
            </div>
          </div>
        ) : null}

        <div className="flex flex-wrap gap-3">
          <Button
            type="button"
            onClick={() => { createMutation.mutate(); }}
            disabled={busy || (requiresApiKey && !apiKey.trim())}
            className="bg-blue-600 hover:bg-blue-700"
          >
            {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            Create draft
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => { testMutation.mutate(); }}
            disabled={busy || !canTest}
            className="border-slate-600 text-slate-200 hover:text-white"
          >
            {testMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <CheckCircle className="h-4 w-4" />
            )}
            Test proving
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => { enableMutation.mutate(); }}
            disabled={busy || !canEnable}
            className="border-slate-600 text-slate-200 hover:text-white"
          >
            Enable
          </Button>
        </div>

        <DeploymentPicker
          deploymentRef={deploymentRef}
          runnable={runnable}
          lifecycleState={lifecycleState}
          onSelect={() => { selectMutation.mutate(); }}
          isPending={selectMutation.isPending}
        />
      </CardContent>
    </Card>
  );
}

export default ConnectionSettingsPanel;
