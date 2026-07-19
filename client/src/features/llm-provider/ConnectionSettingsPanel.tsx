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
  createLLMManagedConnection,
  createLLMProvingConnection,
  enableLLMManagedConnection,
  enableLLMProvingConnection,
  refreshLLMManagedConnectionInventory,
  testLLMManagedConnection,
  testLLMProvingConnection,
} from "@/features/llm-provider/api";
import type {
  LLMCatalogModel,
  LLMConnectionMetadata,
  LLMConnectionRef,
  LLMDeploymentRef,
  LLMDeploymentStatusOverride,
  LLMManagedConnectionCreateRequest,
  LLMManagedConnectionRefreshRequest,
  LLMProvingConnectionCreateRequest,
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
  connection: LLMConnectionMetadata | LLMProvingMetadata;
  usesProvingRoutes?: boolean;
  onDeploymentStatusChange?: (status: LLMDeploymentStatusOverride) => void;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;

export function ConnectionSettingsPanel({
  model,
  connection,
  usesProvingRoutes = false,
  onDeploymentStatusChange,
  onSuccess,
  onError,
}: ConnectionSettingsPanelProps) {
  const queryClient = useQueryClient();
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [connectionRef, setConnectionRef] = useState<LLMConnectionRef | null>(
    connection.connectionRef ?? null,
  );
  const [deploymentRef, setDeploymentRef] = useState<LLMDeploymentRef | null>(
    connection.deploymentRef ?? model.deploymentRef ?? null,
  );
  const [lifecycleState, setLifecycleState] = useState(
    connection.lifecycleState || "unknown",
  );
  const [verification, setVerification] = useState<LLMProvingVerification | null>(
    connection.verification ?? null,
  );
  const [runnable, setRunnable] = useState(
    Boolean(connection.runnability?.runnable ?? model.runnable),
  );
  const [runnabilityStatus, setRunnabilityStatus] = useState(
    connection.runnability?.status ?? "unknown",
  );
  const configFields = connection.configFields?.length
    ? connection.configFields
    : connection.userConfigFields.map((name) => ({
        name,
        label: name === "api_key" ? "Proving API Key" : name,
        fieldType: name === "api_key" ? "password" : "text",
        required: name === "api_key",
        secret: name === "api_key",
      }));

  const requiresApiKey = useMemo(
    () => configFields.some((field) => field.name === "api_key" && field.required),
    [configFields],
  );
  const apiKey = fieldValues.api_key ?? "";
  const requiredFieldsComplete = configFields.every((field) =>
    !field.required || Boolean((fieldValues[field.name] ?? "").trim()),
  );
  const canTest = Boolean((apiKey.trim() || !requiresApiKey) && connectionRef);
  const canEnable = verification?.status === "passed" && Boolean(connectionRef && deploymentRef);

  const invalidateCatalog = async () => {
    await queryClient.invalidateQueries({ queryKey: catalogQueryKey });
  };

  const publishDeploymentStatus = (
    nextDeploymentRef: LLMDeploymentRef | null | undefined,
    status: {
      lifecycleState?: string | null;
      runnable?: boolean | null;
      runnabilityStatus?: string | null;
      reason?: string | null;
    },
  ) => {
    if (!nextDeploymentRef) {
      return;
    }
    onDeploymentStatusChange?.({
      deploymentRef: nextDeploymentRef,
      lifecycleState: status.lifecycleState,
      runnable: status.runnable,
      status: status.runnabilityStatus,
      reason: status.reason,
    });
  };

  const createMutation = useMutation({
    mutationFn: () => {
      const displayLabel = fieldValues.display_label?.trim() || "";
      if (usesProvingRoutes) {
        const request: LLMProvingConnectionCreateRequest = {
          api_key: apiKey.trim() || null,
        };
        if (displayLabel) {
          request.display_label = displayLabel;
        }
        return createLLMProvingConnection(connection.presetId, request);
      }

      const request: LLMManagedConnectionCreateRequest = {
        api_key: apiKey.trim() || null,
        display_label: displayLabel || null,
        base_url: fieldValues.base_url?.trim() || null,
        wire_model_id: fieldValues.wire_model_id?.trim() || model.exactWireModelId || model.id,
        model_label: model.label,
        canonical_model_id: managedCanonicalModelId(model, connection),
      };
      return createLLMManagedConnection(connection.presetId, request);
    },
    onSuccess: async (status) => {
      setConnectionRef(status.connectionRef ?? connectionRef);
      setDeploymentRef(status.deploymentRef ?? deploymentRef);
      setLifecycleState(status.lifecycleState);
      if (status.verification) {
        setVerification(status.verification);
      }
      const nextRunnable = Boolean(status.runnability?.runnable ?? false);
      const nextStatus = status.runnability?.status ?? "unknown";
      setRunnable(nextRunnable);
      setRunnabilityStatus(nextStatus);
      publishDeploymentStatus(status.deploymentRef ?? deploymentRef, {
        lifecycleState: status.lifecycleState,
        runnable: nextRunnable,
        runnabilityStatus: nextStatus,
        reason: status.runnability?.reason,
      });
      await invalidateCatalog();
      onSuccess(
        usesProvingRoutes ? "Proving draft created" : "Connection draft created",
        usesProvingRoutes
          ? "The proving connection draft is ready."
          : "The connection draft is ready.",
      );
    },
    onError: (error) => onError(
      usesProvingRoutes ? "Proving draft failed" : "Connection draft failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const testMutation = useMutation({
    mutationFn: () => usesProvingRoutes
      ? testLLMProvingConnection(connection.presetId, {
          api_key: apiKey.trim(),
          connection_ref: connectionRef,
          deployment_ref: deploymentRef,
        })
      : testLLMManagedConnection(connection.presetId, {
          api_key: apiKey.trim() || null,
          connection_ref: connectionRef,
        }),
    onSuccess: async (result) => {
      setVerification(result);
      await invalidateCatalog();
      onSuccess(
        usesProvingRoutes ? "Proving connection verified" : "Connection verified",
        result.message,
      );
    },
    onError: (error) => onError(
      usesProvingRoutes ? "Proving connection test failed" : "Connection test failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const refreshMutation = useMutation({
    mutationFn: () => {
      const request: LLMManagedConnectionRefreshRequest = {
        api_key: apiKey.trim() || null,
        connection_ref: connectionRef as LLMConnectionRef,
      };
      return refreshLLMManagedConnectionInventory(connection.presetId, request);
    },
    onSuccess: async (status) => {
      setLifecycleState(status.lifecycleState);
      setConnectionRef(status.connectionRef ?? connectionRef);
      setDeploymentRef(status.deploymentRef ?? deploymentRef);
      const nextRunnable = Boolean(status.runnability?.runnable ?? runnable);
      const nextStatus = status.runnability?.status ?? runnabilityStatus;
      setRunnable(nextRunnable);
      setRunnabilityStatus(nextStatus);
      publishDeploymentStatus(status.deploymentRef ?? deploymentRef, {
        lifecycleState: status.lifecycleState,
        runnable: nextRunnable,
        runnabilityStatus: nextStatus,
        reason: status.runnability?.reason,
      });
      await invalidateCatalog();
      onSuccess("Inventory refreshed", "Backend inventory is updated for this connection.");
    },
    onError: (error) => onError(
      "Inventory refresh failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const enableMutation = useMutation({
    mutationFn: () => usesProvingRoutes
      ? enableLLMProvingConnection(connection.presetId, {
          connection_ref: connectionRef as LLMConnectionRef,
          deployment_ref: deploymentRef as LLMDeploymentRef,
        })
      : enableLLMManagedConnection(connection.presetId, {
          connection_ref: connectionRef as LLMConnectionRef,
          deployment_ref: deploymentRef,
        }),
    onSuccess: async (status) => {
      setLifecycleState(status.lifecycleState);
      setConnectionRef(status.connectionRef ?? connectionRef);
      setDeploymentRef(status.deploymentRef ?? deploymentRef);
      const nextRunnable = Boolean(status.runnability?.runnable ?? runnable);
      const nextStatus = status.runnability?.status ?? runnabilityStatus;
      setRunnable(nextRunnable);
      setRunnabilityStatus(nextStatus);
      publishDeploymentStatus(status.deploymentRef ?? deploymentRef, {
        lifecycleState: status.lifecycleState,
        runnable: nextRunnable,
        runnabilityStatus: nextStatus,
        reason: status.runnability?.reason,
      });
      await invalidateCatalog();
      onSuccess(
        usesProvingRoutes ? "Proving connection enabled" : "Connection enabled",
        usesProvingRoutes
          ? "The proving deployment can be selected."
          : "The managed deployment can be selected.",
      );
    },
    onError: (error) => onError(
      usesProvingRoutes ? "Proving enable failed" : "Connection enable failed",
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const busy =
    createMutation.isPending ||
    testMutation.isPending ||
    refreshMutation.isPending ||
    enableMutation.isPending;

  return (
    <Card className="border-slate-700 bg-slate-900">
      <CardHeader className="space-y-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <CardTitle className="flex items-center text-base text-white">
              <ShieldCheck className="mr-2 h-4 w-4" />
              {connection.displayName}
            </CardTitle>
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
              <Badge className="bg-slate-700 text-slate-200">
                Lifecycle: {lifecycleState}
              </Badge>
              <Badge className="bg-slate-700 text-slate-200">
                Verification: {verification?.code ?? "not_tested"}
              </Badge>
              <Badge className="bg-slate-700 text-slate-200">
                Runnability: {runnabilityStatus}
              </Badge>
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {configFields.map((field) => (
          <div key={field.name}>
            <Label htmlFor={`llm-connection-${field.name}-${connection.presetId}`} className="text-white">
              {usesProvingRoutes && field.name === "api_key" ? "Proving API Key" : field.label}
            </Label>
            <div className="relative mt-2">
              <Input
                id={`llm-connection-${field.name}-${connection.presetId}`}
                type={field.fieldType === "password" ? "password" : field.fieldType === "url" ? "url" : "text"}
                value={fieldValues[field.name] ?? ""}
                onChange={(event) =>
                  setFieldValues((current) => ({
                    ...current,
                    [field.name]: event.target.value,
                  }))
                }
                autoComplete="off"
                className="border-slate-600 bg-slate-800 pr-10 text-white"
              />
              {field.secret ? (
                <Key className="absolute right-3 top-2.5 h-4 w-4 text-slate-500" />
              ) : null}
            </div>
          </div>
        ))}

        <div className="flex flex-wrap gap-3">
          <Button
            type="button"
            onClick={() => { createMutation.mutate(); }}
            disabled={busy || !requiredFieldsComplete}
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
            {usesProvingRoutes ? "Test proving" : "Test connection"}
          </Button>
          {!usesProvingRoutes ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => { refreshMutation.mutate(); }}
              disabled={busy || !connectionRef}
              className="border-slate-600 text-slate-200 hover:text-white"
            >
              {refreshMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              Refresh inventory
            </Button>
          ) : null}
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
      </CardContent>
    </Card>
  );
}

function managedCanonicalModelId(
  model: LLMCatalogModel,
  connection: LLMConnectionMetadata | LLMProvingMetadata,
): string | null {
  const canonical = model.canonicalModelId?.trim();
  if (!canonical || canonical === model.id || canonical === connection.presetId) {
    return null;
  }
  return canonical;
}

export default ConnectionSettingsPanel;
