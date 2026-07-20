/**
 * Metadata-driven managed connection controls for one approved LLM preset.
 *
 * The panel renders only backend-declared user configuration fields and never
 * accepts headers or arbitrary provider inventory values.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Key, Loader2 } from "lucide-react";

import {
  createLLMManagedConnection,
  enableLLMManagedConnection,
  refreshLLMManagedConnectionInventory,
  testLLMManagedConnection,
} from "@/features/llm-provider/api";
import type {
  LLMCatalogModel,
  LLMConnectionMetadata,
  LLMConnectionRef,
  LLMDeploymentRef,
  LLMManagedConnectionCreateRequest,
  LLMProvingConnectionStatus,
} from "@/features/llm-provider/types";
import {
  ProviderApiKeyField,
  ProviderSettingsCard,
} from "@/features/llm-provider/ProviderSettingsCard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface ConnectionSettingsPanelProps {
  model: LLMCatalogModel;
  connection: LLMConnectionMetadata;
  setupNote?: string | null;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;

export function ConnectionSettingsPanel({
  model,
  connection,
  setupNote = null,
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
  const [runnable, setRunnable] = useState(
    Boolean(connection.runnability?.runnable ?? model.runnable),
  );
  const configFields = connection.configFields?.length
    ? connection.configFields
    : connection.userConfigFields.map((name) => ({
        name,
        label: fallbackFieldLabel(name),
        fieldType: name === "api_key" ? "password" : "text",
        required: name === "api_key",
        secret: name === "api_key",
      }));
  const visibleConfigFields = configFields
    .filter((field) => field.name !== "display_label")
    .map((field) => {
      if (field.name === "wire_model_id") {
        return { ...field, label: "Model name" };
      }
      return field;
    });

  const apiKey = fieldValues.api_key ?? "";
  const requiredFieldsComplete = visibleConfigFields.every((field) =>
    !field.required || Boolean((fieldValues[field.name] ?? "").trim()),
  );

  const invalidateCatalog = async () => {
    await queryClient.invalidateQueries({ queryKey: catalogQueryKey });
  };

  const applyConnectionStatus = (status: LLMProvingConnectionStatus) => {
    setConnectionRef(status.connectionRef ?? connectionRef);
    setDeploymentRef(status.deploymentRef ?? deploymentRef);
    setRunnable(Boolean(status.runnability?.runnable ?? runnable));
  };

  const connectMutation = useMutation({
    mutationFn: async () => {
      const createRequest: LLMManagedConnectionCreateRequest = {
        api_key: apiKey.trim() || null,
        display_label: null,
        base_url: fieldValues.base_url?.trim() || null,
        wire_model_id: fieldValues.wire_model_id?.trim() || model.exactWireModelId || model.id,
        model_label: model.label,
        canonical_model_id: managedCanonicalModelId(model, connection),
      };
      const created = await createLLMManagedConnection(connection.presetId, createRequest);
      let nextConnectionRef = created.connectionRef ?? connectionRef;
      let nextDeploymentRef = created.deploymentRef ?? deploymentRef;
      if (!nextConnectionRef) {
        return created;
      }

      const verified = await testLLMManagedConnection(connection.presetId, {
        api_key: apiKey.trim() || null,
        connection_ref: nextConnectionRef,
      });

      let connectionStatus = created;
      if (!nextDeploymentRef) {
        const refreshed = await refreshLLMManagedConnectionInventory(connection.presetId, {
          api_key: apiKey.trim() || null,
          connection_ref: nextConnectionRef,
        });
        connectionStatus = refreshed;
        nextConnectionRef = refreshed.connectionRef ?? nextConnectionRef;
        nextDeploymentRef = refreshed.deploymentRef ?? nextDeploymentRef;
      }

      if (verified.status === "passed" && nextConnectionRef && nextDeploymentRef) {
        return enableLLMManagedConnection(connection.presetId, {
          connection_ref: nextConnectionRef,
          deployment_ref: nextDeploymentRef,
        });
      }
      return { ...connectionStatus, verification: verified };
    },
    onSuccess: async (status) => {
      applyConnectionStatus(status);
      await invalidateCatalog();
      onSuccess(
        `${connection.displayName} connected`,
        runnable || status.runnability?.runnable
          ? "GPT-OSS 20B is ready."
          : "The connection was saved, but GPT-OSS 20B is not ready yet.",
      );
    },
    onError: (error) => onError(
      `${connection.displayName} connection failed`,
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const statusLabel = runnable ? "Ready" : connectionRef ? "Connected" : "Not connected";

  return (
    <ProviderSettingsCard
      title={connection.displayName}
      setupNote={setupNote}
      statusLabel={statusLabel}
      statusPositive={Boolean(runnable || connectionRef)}
    >
      {visibleConfigFields.map((field) => (
        field.name === "api_key" ? (
          <ProviderApiKeyField
            key={field.name}
            id={`llm-connection-${field.name}-${connection.presetId}`}
            value={fieldValues[field.name] ?? ""}
            onChange={(value) =>
              setFieldValues((current) => ({ ...current, [field.name]: value }))
            }
            placeholder={
              connectionRef
                ? "Enter a new API key to update"
                : "Enter provider API key"
            }
            label="API Key"
          />
        ) : (
          <div key={field.name}>
            <Label htmlFor={`llm-connection-${field.name}-${connection.presetId}`} className="text-white">
              {field.label}
            </Label>
            <div className="relative mt-2">
              <Input
                id={`llm-connection-${field.name}-${connection.presetId}`}
                type={
                  field.fieldType === "password"
                    ? "password"
                    : field.fieldType === "url"
                      ? "url"
                      : "text"
                }
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
        )
      ))}

      <div className="flex flex-wrap gap-3">
        <Button
          type="button"
          aria-label={`${connectionRef ? "Update" : "Connect"} ${connection.displayName}`}
          onClick={() => { connectMutation.mutate(); }}
          disabled={connectMutation.isPending || !requiredFieldsComplete}
          className="bg-blue-600 hover:bg-blue-700"
        >
          {connectMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Key className="h-4 w-4" />
          )}
          {connectionRef ? "Update" : "Connect"}
        </Button>
      </div>
    </ProviderSettingsCard>
  );
}

function managedCanonicalModelId(
  model: LLMCatalogModel,
  connection: LLMConnectionMetadata,
): string | null {
  const canonical = model.canonicalModelId?.trim();
  if (
    canonical === "openai/gpt-oss-20b"
    && [
      "huggingface_openai_compatible_chat",
      "nvidia_nim_openai_compatible_chat",
      "ollama_openai_compatible_chat",
      "vllm_openai_compatible_chat",
    ].includes(connection.presetId)
  ) {
    return canonical;
  }
  if (!canonical || canonical === model.id || canonical === connection.presetId) {
    return null;
  }
  return canonical;
}

function fallbackFieldLabel(name: string): string {
  if (name === "api_key") {
    return "API key";
  }
  if (name === "display_label") {
    return "Display name";
  }
  return name;
}

export default ConnectionSettingsPanel;
