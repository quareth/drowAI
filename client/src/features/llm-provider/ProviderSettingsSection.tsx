/**
 * Provider-neutral LLM settings section.
 *
 * Keeps direct provider credentials separate from the intentionally supported
 * GPT-OSS 20B hosted and self-hosted routes.
 */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";

import { fetchLLMModelCatalog } from "@/features/llm-provider/api";
import ConnectionSettingsPanel from "@/features/llm-provider/ConnectionSettingsPanel";
import ProviderCredentialCard from "@/features/llm-provider/ProviderCredentialCard";
import type {
  LLMCatalogModel,
  LLMCatalogProvider,
  LLMConnectionMetadata,
  LLMModelCatalogResponse,
} from "@/features/llm-provider/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export interface ProviderSettingsSectionProps {
  queryEnabled: boolean;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;
interface ConnectionSettingsEntry {
  model: LLMCatalogModel;
  connection: LLMConnectionMetadata;
}

const publicOpenModelPresetNames: Record<string, string> = {
  huggingface_openai_compatible_chat: "Hugging Face",
  nvidia_nim_openai_compatible_chat: "NVIDIA",
  ollama_openai_compatible_chat: "Ollama",
  vllm_openai_compatible_chat: "vLLM",
};

function toErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "The provider settings request failed.";
}

export function ProviderSettingsSection({
  queryEnabled,
  onSuccess,
  onError,
}: ProviderSettingsSectionProps) {
  const queryClient = useQueryClient();
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const {
    data: catalog,
    error: catalogError,
    isError: catalogIsError,
    isLoading: catalogLoading,
    refetch: refetchCatalog,
  } = useQuery<LLMModelCatalogResponse>({
    queryKey: catalogQueryKey,
    queryFn: fetchLLMModelCatalog,
    enabled: queryEnabled,
  });
  const providers = catalog?.providers ?? [];
  const connectionModels = getConnectionSettingsEntries(providers);
  const hostedConnectionModels = connectionModels.filter(({ connection }) =>
    isHostedConnection(connection),
  );
  const advancedConnectionModels = connectionModels.filter(({ connection }) =>
    !isHostedConnection(connection),
  );
  const credentialProviders = providers.filter((provider) =>
    isDirectHostedCredentialProvider(provider.id),
  );

  const handleProviderSuccess = (title: string, description: string) => {
    void queryClient.invalidateQueries({ queryKey: catalogQueryKey });
    onSuccess(title, description);
  };

  return (
    <div className="space-y-6">
      {catalogLoading ? (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
        </div>
      ) : catalogIsError ? (
        <Alert className="border-red-800/60 bg-red-950/30 text-red-100">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Unable to load LLM providers</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>{toErrorMessage(catalogError)}</p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="border-red-700/70 text-red-100 hover:text-white"
              onClick={() => { void refetchCatalog(); }}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      ) : providers.length === 0 ? (
        <Alert className="border-slate-700 bg-slate-800 text-slate-200">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>No providers available</AlertTitle>
          <AlertDescription>
            No LLM providers are currently available in the backend catalog.
          </AlertDescription>
        </Alert>
      ) : (
        <div className="space-y-6">
          {credentialProviders.length > 0 ? (
            <section className="space-y-3">
              <h3 className="text-sm font-semibold text-white">AI providers</h3>
              <div className="grid gap-4 lg:grid-cols-2">
                {credentialProviders.map((provider) => (
                  <ProviderCredentialCard
                    key={provider.id}
                    provider={provider}
                    setupNote={providerCredentialNote(provider.id)}
                    onSuccess={handleProviderSuccess}
                    onError={onError}
                  />
                ))}
              </div>
            </section>
          ) : null}

          {hostedConnectionModels.length > 0 ? (
            <section className="space-y-3">
              <div>
                <h3 className="text-sm font-semibold text-white">Open models</h3>
                <p className="mt-1 text-xs text-slate-400">
                  Connect GPT-OSS 20B to a hosted service.
                </p>
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                {hostedConnectionModels.map(({ model, connection }) => (
                  <ConnectionSettingsPanel
                    key={connectionSettingsKey(connection)}
                    model={model}
                    connection={connection}
                    setupNote={connectionSetupNote(connection.presetId)}
                    showOperationalDetails={false}
                    onSuccess={handleProviderSuccess}
                    onError={onError}
                  />
                ))}
              </div>
            </section>
          ) : null}

          {advancedConnectionModels.length > 0 ? (
            <section className="space-y-3">
              <Button
                type="button"
                variant="outline"
                aria-expanded={advancedOpen}
                onClick={() => setAdvancedOpen((current) => !current)}
                className="border-slate-700 text-slate-200 hover:text-white"
              >
                Local &amp; self-hosted
              </Button>
              {advancedOpen ? (
                <div className="space-y-4">
                  {advancedConnectionModels.map(({ model, connection }) => (
                    <ConnectionSettingsPanel
                      key={connectionSettingsKey(connection)}
                      model={model}
                      connection={connection}
                      setupNote="Run GPT-OSS 20B through your own HTTPS endpoint."
                      showOperationalDetails={false}
                      onSuccess={handleProviderSuccess}
                      onError={onError}
                    />
                  ))}
                </div>
              ) : null}
            </section>
          ) : null}
        </div>
      )}
    </div>
  );
}

export default ProviderSettingsSection;

function getConnectionSettingsEntries(
  providers: LLMCatalogProvider[],
): ConnectionSettingsEntry[] {
  const entriesByConnection = new Map<string, ConnectionSettingsEntry>();
  for (const provider of providers) {
    for (const model of provider.models) {
      const entry = getConnectionSettingsEntry(model);
      if (!entry) {
        continue;
      }
      const key = connectionSettingsKey(entry.connection);
      const current = entriesByConnection.get(key);
      if (!current || connectionSettingsScore(entry) > connectionSettingsScore(current)) {
        entriesByConnection.set(key, entry);
      }
    }
  }
  return Array.from(entriesByConnection.values());
}

function getConnectionSettingsEntry(
  model: LLMCatalogModel,
): ConnectionSettingsEntry | null {
  const connection = model.connection;
  const displayName = connection
    ? publicOpenModelPresetNames[connection.presetId]
    : null;
  if (
    connection?.enabled
    && displayName
    && model.canonicalModelId?.trim().toLowerCase() === "openai/gpt-oss-20b"
  ) {
    return {
      model,
      connection: { ...connection, displayName },
    };
  }
  return null;
}

function connectionSettingsKey(connection: LLMConnectionMetadata): string {
  return connection.presetId;
}

function connectionSettingsScore(entry: ConnectionSettingsEntry): number {
  const { connection, model } = entry;
  let score = 0;
  if (connection.connectionRef) {
    score += 100;
  }
  if (connection.deploymentRef || model.deploymentRef) {
    score += 50;
  }
  if (connection.runnability) {
    score += 20;
  }
  if (connection.runnability?.runnable) {
    score += 20;
  }
  if (connection.verification) {
    score += 10;
  }
  if (connection.lifecycleState && !["not_created", "unknown"].includes(connection.lifecycleState)) {
    score += 5;
  }
  if (model.runnable) {
    score += 1;
  }
  return score;
}

function isHostedConnection(
  connection: LLMConnectionMetadata,
): boolean {
  return !connection.configFields?.some((field) => field.name === "base_url");
}

function connectionSetupNote(presetId: string): string | null {
  if (presetId === "huggingface_openai_compatible_chat") {
    return "Credits and pay-as-you-go usage apply.";
  }
  if (presetId === "nvidia_nim_openai_compatible_chat") {
    return "Free development and prototyping access has usage limits.";
  }
  return "Enter an API key. Endpoint and model details are managed by DrowAI.";
}

function providerCredentialNote(providerId: string): string | null {
  if (providerId === "openai") {
    return "Usage is billed by OpenAI for the selected model.";
  }
  if (providerId === "anthropic") {
    return "Usage is billed by Anthropic for the selected model.";
  }
  return null;
}

function isDirectHostedCredentialProvider(providerId: string): boolean {
  return providerId === "openai" || providerId === "anthropic";
}
