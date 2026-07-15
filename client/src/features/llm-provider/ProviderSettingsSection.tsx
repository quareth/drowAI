/**
 * Provider-neutral LLM settings section.
 *
 * Composes reusable provider credential cards for OpenAI, Anthropic, and
 * future registered providers.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";

import {
  fetchLLMModelCatalog,
  fetchReportingLLMSelection,
  saveReportingLLMSelection,
} from "@/features/llm-provider/api";
import {
  findSelectedCatalogEntry,
} from "@/features/llm-provider/catalog";
import {
  getDefaultVisibleReasoningEffort,
  getSupportedReasoningEffortForPayload,
} from "@/features/llm-provider/capability-controls";
import ProviderCredentialCard from "@/features/llm-provider/ProviderCredentialCard";
import ProviderModelMenu from "@/features/llm-provider/ProviderModelMenu";
import type {
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
  const selectedReportingModel =
    reportingSelection?.provider && reportingSelection.model
      ? {
          provider: reportingSelection.provider,
          model: reportingSelection.model,
        }
      : null;
  const selectedEntry = findSelectedCatalogEntry(catalog, selectedReportingModel);
  const reportingReasoningEffort =
    coerceVisibleReasoningEffort(reportingSelection?.reasoningEffort) ??
    getDefaultVisibleReasoningEffort(selectedEntry?.model) ??
    "medium";
  const reportingStatus = reportingSelection?.selectionStatus;
  const reportingStatusMessage =
    reportingStatus?.runnable === false
      ? reportingStatus.reason ?? "Reporting model is not ready."
      : selectedReportingModel
        ? "Ready for task memos and engagement reports."
        : "Choose a model before generating task memos or engagement reports.";

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
      provider: selection.provider,
      model: selection.model,
      reasoning_effort: reasoningEffort ?? null,
    });
  };

  const handleProviderSuccess = (title: string, description: string) => {
    void queryClient.invalidateQueries({ queryKey: reportingSelectionQueryKey });
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
        <div className="space-y-4">
          <section className="rounded-lg border border-slate-800 bg-slate-900 p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold text-white">Reporting model</h3>
                <p className="mt-1 text-xs text-slate-400">
                  Used for task memos and engagement reports.
                </p>
                <p
                  className={
                    reportingStatus?.runnable === false
                      ? "mt-2 text-xs text-amber-200"
                      : "mt-2 text-xs text-emerald-200"
                  }
                  aria-live="polite"
                >
                  {reportingStatusMessage}
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

          <div className="grid gap-4 lg:grid-cols-2">
            {providers.map((provider) => (
              <ProviderCredentialCard
                key={provider.id}
                provider={provider}
                onSuccess={handleProviderSuccess}
                onError={onError}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function coerceVisibleReasoningEffort(
  value: unknown,
): VisibleLLMReasoningEffort | null {
  return value === "low" || value === "medium" || value === "high" || value === "xhigh" || value === "max"
    ? value
    : null;
}

export default ProviderSettingsSection;
