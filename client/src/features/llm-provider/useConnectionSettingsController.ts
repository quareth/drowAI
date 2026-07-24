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
  disconnectLLMManagedConnection,
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
  hasStoredCredential: boolean;
  onConnected: (ready: boolean) => void;
  onConnectionError: (error: Error) => void;
  onDisconnected: () => void;
  onDisconnectError: (error: Error) => void;
}

interface ConnectionSettingsController {
  connected: boolean;
  connectionRef: LLMConnectionRef | null;
  runnable: boolean;
  isPending: boolean;
  connect: () => void;
  disconnect: () => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;

export function useConnectionSettingsController({
  model,
  connection,
  fieldValues,
  hasStoredCredential,
  onConnected,
  onConnectionError,
  onDisconnected,
  onDisconnectError,
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
  const [connected, setConnected] = useState(
    Boolean(hasStoredCredential && connection.connectionRef),
  );
  const apiKey = fieldValues.api_key ?? "";

  const invalidateCatalog = async () => {
    await queryClient.invalidateQueries({ queryKey: catalogQueryKey });
  };

  const applyConnectionStatus = (status: LLMProvingConnectionStatus) => {
    setConnectionRef(status.connectionRef ?? connectionRef);
    setDeploymentRef(status.deploymentRef ?? deploymentRef);
    setRunnable(Boolean(status.runnability?.runnable ?? runnable));
    setConnected(Boolean(status.connectionRef ?? connectionRef));
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
        canonical_model_id: managedCanonicalModelId(model),
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
      onConnected(Boolean(runnable || status.runnability?.runnable));
    },
    onError: (error) => onConnectionError(
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const disconnectMutation = useMutation({
    mutationFn: async () => {
      if (!connectionRef) {
        throw new Error("Connection reference is unavailable.");
      }
      return disconnectLLMManagedConnection(connection.presetId, {
        connection_ref: connectionRef,
      });
    },
    onSuccess: async () => {
      setConnectionRef(null);
      setRunnable(false);
      setConnected(false);
      await invalidateCatalog();
      onDisconnected();
    },
    onError: (error) => onDisconnectError(
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  return {
    connected,
    connectionRef,
    runnable,
    isPending: connectMutation.isPending || disconnectMutation.isPending,
    connect: () => { connectMutation.mutate(); },
    disconnect: () => { disconnectMutation.mutate(); },
  };
}

function managedCanonicalModelId(
  model: LLMCatalogModel,
): string | null {
  const canonical = model.canonicalModelId?.trim();
  if (!canonical || canonical === model.id) {
    return null;
  }
  return canonical;
}
