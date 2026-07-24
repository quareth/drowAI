/**
 * Provider-neutral LLM settings section.
 *
 * Renders direct and reviewed hosted providers together while keeping
 * self-hosted compatible routes behind their explicit visibility gate.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";

import {
  fetchLLMModelCatalog,
  fetchReportingLLMSelection,
  saveReportingLLMSelection,
} from "@/features/llm-provider/api";
import { findSelectedCatalogEntry } from "@/features/llm-provider/catalog";
import {
  getDefaultVisibleReasoningEffort,
  getSupportedReasoningEffortForPayload,
} from "@/features/llm-provider/capability-controls";
import ConnectionSettingsPanel from "@/features/llm-provider/ConnectionSettingsPanel";
import ProviderCredentialCard from "@/features/llm-provider/ProviderCredentialCard";
import ProviderModelMenu from "@/features/llm-provider/ProviderModelMenu";
import { isIncompleteSelfHostedProviderVisible } from "@/features/llm-provider/self-hosted-visibility";
import type {
  LLMCatalogModel,
  LLMCatalogProvider,
  LLMConnectionMetadata,
  LLMModelCatalogResponse,
  ReportingLLMSelection,
  SelectedLLMModel,
  VisibleLLMReasoningEffort,
} from "@/features/llm-provider/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export interface ProviderSettingsSectionProps {
  queryEnabled: boolean;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;
const reportingSelectionQueryKey = ["/api/llm/reporting-selection"] as const;
interface ConnectionSettingsEntry {
  providerLabel: string;
  model: LLMCatalogModel;
  connection: LLMConnectionMetadata;
  hasStoredCredential: boolean;
}

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
  const { data: reportingSelection } = useQuery<ReportingLLMSelection>({
    queryKey: reportingSelectionQueryKey,
    queryFn: fetchReportingLLMSelection,
    enabled: queryEnabled,
  });
  const saveReportingSelection = useMutation({
    mutationFn: saveReportingLLMSelection,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: reportingSelectionQueryKey });
      onSuccess(
        "Reporting model updated",
        "Task memos and engagement reports will use this model.",
      );
    },
    onError: (error: Error) => {
      onError("Reporting model update failed", error);
    },
  });
  const providers = catalog?.providers ?? [];
  const connectionModels = getConnectionSettingsEntries(providers).filter(
    ({ connection }) => isIncompleteSelfHostedProviderVisible(connection.presetId),
  );
  const hostedConnectionModels = connectionModels.filter(({ connection }) =>
    isHostedConnection(connection),
  );
  const advancedConnectionModels = connectionModels.filter(({ connection }) =>
    !isHostedConnection(connection),
  );
  const credentialProviders = providers.filter((provider) =>
    provider.models.every((model) => !model.connection?.enabled),
  );
  const selectedReportingModel =
    reportingSelection?.provider && reportingSelection.model
      ? {
          provider: reportingSelection.provider,
          model: reportingSelection.model,
          deploymentRef: reportingSelection.deploymentRef ?? null,
        }
      : null;
  const selectedReportingEntry = findSelectedCatalogEntry(catalog, selectedReportingModel);
  const reportingReasoningEffort =
    coerceVisibleReasoningEffort(reportingSelection?.reasoningEffort) ??
    getDefaultVisibleReasoningEffort(selectedReportingEntry?.model) ??
    "medium";

  const handleReportingModelChange = (
    selection: SelectedLLMModel,
    options?: { reasoningEffort?: VisibleLLMReasoningEffort },
  ) => {
    const reasoningEffort = getSupportedReasoningEffortForPayload(
      catalog,
      selection,
      options?.reasoningEffort ?? reportingReasoningEffort,
    );
    saveReportingSelection.mutate({
      ...selection,
      reasoning_effort: reasoningEffort ?? null,
    });
  };

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
          <section className="rounded-lg border border-slate-800 bg-slate-900 p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold text-white">Reporting model</h3>
                <p className="mt-1 text-xs text-slate-400">
                  Used for task memos and engagement reports.
                </p>
              </div>
              <ProviderModelMenu
                catalog={catalog}
                selectedSelection={selectedReportingModel}
                selectedReasoningEffort={reportingReasoningEffort}
                onModelChange={handleReportingModelChange}
                className="h-8 min-w-[230px]"
              />
            </div>
          </section>

          {credentialProviders.length > 0 || hostedConnectionModels.length > 0 ? (
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
                {hostedConnectionModels.map(({
                  providerLabel,
                  model,
                  connection,
                  hasStoredCredential,
                }) => (
                  <ConnectionSettingsPanel
                    key={connectionSettingsKey(connection)}
                    providerLabel={providerLabel}
                    model={model}
                    connection={connection}
                    hasStoredCredential={hasStoredCredential}
                    setupNote={connectionSetupNote(connection.presetId)}
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
                  {advancedConnectionModels.map(({
                    providerLabel,
                    model,
                    connection,
                    hasStoredCredential,
                  }) => (
                    <ConnectionSettingsPanel
                      key={connectionSettingsKey(connection)}
                      providerLabel={providerLabel}
                      model={model}
                      connection={connection}
                      hasStoredCredential={hasStoredCredential}
                      setupNote="Run GPT-OSS 20B through your own HTTPS endpoint."
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

function coerceVisibleReasoningEffort(
  value: unknown,
): VisibleLLMReasoningEffort | null {
  return value === "none" || value === "low" || value === "medium" || value === "high" || value === "xhigh" || value === "max"
    ? value
    : null;
}

export default ProviderSettingsSection;

function getConnectionSettingsEntries(
  providers: LLMCatalogProvider[],
): ConnectionSettingsEntry[] {
  const entriesByConnection = new Map<string, ConnectionSettingsEntry>();
  for (const provider of providers) {
    for (const model of provider.models) {
      const entry = getConnectionSettingsEntry(provider, model);
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
  provider: LLMCatalogProvider,
  model: LLMCatalogModel,
): ConnectionSettingsEntry | null {
  const connection = model.connection;
  if (connection?.enabled) {
    return {
      providerLabel: provider.label,
      model,
      connection,
      hasStoredCredential: (
        provider.credential.enabled && provider.credential.has_api_key
      ),
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
