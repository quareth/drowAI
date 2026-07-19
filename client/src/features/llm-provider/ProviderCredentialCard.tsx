/**
 * Reusable credential editor for one LLM provider.
 *
 * Handles non-secret credential status and one-action connect/disconnect
 * operations through provider-neutral LLM APIs without echoing stored keys.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Key, Loader2, Trash2 } from "lucide-react";

import {
  deleteLLMProviderCredential,
  saveLLMProviderCredential,
  testLLMProviderCredential,
} from "@/features/llm-provider/api";
import type { LLMCatalogProvider } from "@/features/llm-provider/types";
import {
  ProviderApiKeyField,
  ProviderSettingsCard,
} from "@/features/llm-provider/ProviderSettingsCard";
import { Button } from "@/components/ui/button";

export interface ProviderCredentialCardProps {
  provider: LLMCatalogProvider;
  setupNote?: string | null;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;

function getCredentialPlaceholder(provider: LLMCatalogProvider): string {
  if (provider.credential.has_api_key) {
    return "Enter a new API key to update";
  }
  return provider.id === "openai" ? "sk-..." : "Enter provider API key";
}

export function ProviderCredentialCard({
  provider,
  setupNote = null,
  onSuccess,
  onError,
}: ProviderCredentialCardProps) {
  const queryClient = useQueryClient();
  const [apiKey, setApiKey] = useState("");

  const invalidateCredentialState = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: catalogQueryKey }),
      queryClient.invalidateQueries({
        queryKey: ["/api/llm/providers", provider.id, "credential"],
      }),
    ]);
  };

  const connectMutation = useMutation({
    mutationFn: async () => {
      const trimmedKey = apiKey.trim();
      if (!trimmedKey) {
        throw new Error("Enter an API key before connecting.");
      }
      const status = await saveLLMProviderCredential(provider.id, {
        api_key: trimmedKey,
        enabled: true,
      });
      await testLLMProviderCredential(provider.id, { api_key: trimmedKey });
      return status;
    },
    onSuccess: async () => {
      setApiKey("");
      await invalidateCredentialState();
      onSuccess(
        `${provider.label} connected`,
        "The provider credential is ready for runtime use.",
      );
    },
    onError: (error) => onError(
      `${provider.label} connection failed`,
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteLLMProviderCredential(provider.id),
    onSuccess: async () => {
      setApiKey("");
      await invalidateCredentialState();
      onSuccess(
        `${provider.label} credential removed`,
        "The provider credential has been disabled for runtime use.",
      );
    },
    onError: (error) => onError(
      `${provider.label} credential delete failed`,
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const hasStoredCredential = provider.credential.enabled && provider.credential.has_api_key;
  const isBusy = connectMutation.isPending || deleteMutation.isPending;

  return (
    <ProviderSettingsCard
      title={provider.label}
      setupNote={setupNote}
      statusLabel={hasStoredCredential ? "Connected" : "Not connected"}
      statusPositive={hasStoredCredential}
    >
      <ProviderApiKeyField
        id={`llm-provider-key-${provider.id}`}
        value={apiKey}
        onChange={setApiKey}
        placeholder={getCredentialPlaceholder(provider)}
      />

      <div className="flex flex-wrap gap-3">
        <Button
          aria-label={`${hasStoredCredential ? "Update" : "Connect"} ${provider.label}`}
          onClick={() => { connectMutation.mutate(); }}
          disabled={isBusy || !apiKey.trim()}
          className="bg-blue-600 hover:bg-blue-700"
        >
          {connectMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Key className="h-4 w-4" />
          )}
          {hasStoredCredential ? "Update" : "Connect"}
        </Button>
        {hasStoredCredential ? (
          <Button
            aria-label={`Disconnect ${provider.label}`}
            onClick={() => { deleteMutation.mutate(); }}
            disabled={isBusy}
            variant="outline"
            className="border-slate-600 text-gray-300 hover:text-white"
          >
            {deleteMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Trash2 className="h-4 w-4" />
            )}
            Disconnect
          </Button>
        ) : null}
      </div>
    </ProviderSettingsCard>
  );
}

export default ProviderCredentialCard;
