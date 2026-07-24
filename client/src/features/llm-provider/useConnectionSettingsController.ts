/**
 * Feature-local managed connection lifecycle controller for one catalog model.
 *
 * Owns managed refs, readiness, request orchestration, catalog invalidation,
 * and callbacks; it must not own rendered fields, labels, visibility rules, or
 * other UI concerns.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import {
  enableLLMManagedConnection,
  refreshLLMManagedConnectionInventory,
  saveLLMManagedConnection,
  testLLMManagedConnection,
} from "@/features/llm-provider/api";
import type {
  LLMCatalogModel,
  LLMConnectionMetadata,
  LLMConnectionRef,
  LLMDeploymentRef,
  LLMManagedConnectionSaveRequest,
  LLMProvingConnectionStatus,
} from "@/features/llm-provider/types";

interface UseConnectionSettingsControllerOptions {
  model: LLMCatalogModel;
  connection: LLMConnectionMetadata;
  fieldValues: Readonly<Record<string, string>>;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

interface ConnectionSettingsController {
  connectionRef: LLMConnectionRef | null;
  runnable: boolean;
  isPending: boolean;
  connect: () => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;

export function useConnectionSettingsController({
  model,
  connection,
  fieldValues,
  onSuccess,
  onError,
}: UseConnectionSettingsControllerOptions): ConnectionSettingsController {
  const queryClient = useQueryClient();
  const [connectionRef, setConnectionRef] = useState<LLMConnectionRef | null>(
    connection.connectionRef ?? null,
  );
  const [deploymentRef, setDeploymentRef] = useState<LLMDeploymentRef | null>(
    connection.deploymentRef ?? model.deploymentRef ?? null,
  );
  const [runnable, setRunnable] = useState(
    Boolean(connection.runnability?.runnable ?? model.runnable),
  );
  const apiKey = fieldValues.api_key ?? "";

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
      const saveRequest: LLMManagedConnectionSaveRequest = {
        api_key: apiKey.trim() || null,
        connection_ref: connectionRef,
        display_label: null,
        base_url: fieldValues.base_url?.trim() || null,
        wire_model_id: fieldValues.wire_model_id?.trim() || model.exactWireModelId || model.id,
        model_label: model.label,
        canonical_model_id: managedCanonicalModelId(model, connection),
      };
      const created = await saveLLMManagedConnection(connection.presetId, saveRequest);
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

  return {
    connectionRef,
    runnable,
    isPending: connectMutation.isPending,
    connect: () => { connectMutation.mutate(); },
  };
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
