/**
 * Pure lookup helpers for provider-neutral LLM catalog data.
 *
 * These functions derive labels and selected model metadata from backend-owned
 * catalog fields without duplicating provider-specific model policy.
 */

import type {
  LLMCatalogModel,
  LLMCatalogProvider,
  LLMDeploymentRef,
  LLMSelection,
  LLMSelectionStatus,
  LLMModelCatalogResponse,
  SelectedLLMModel,
} from "./types";

export interface SelectedCatalogEntry {
  provider: LLMCatalogProvider;
  model: LLMCatalogModel;
}

export function findProvider(
  catalog: LLMModelCatalogResponse | null | undefined,
  providerId: string | null | undefined,
): LLMCatalogProvider | null {
  if (!catalog || typeof providerId !== "string") {
    return null;
  }
  return catalog.providers.find((provider) => provider.id === providerId) ?? null;
}

export function findModel(
  provider: LLMCatalogProvider | null | undefined,
  modelId: string | null | undefined,
): LLMCatalogModel | null {
  if (!provider || typeof modelId !== "string") {
    return null;
  }
  return provider.models.find((model) => model.id === modelId) ?? null;
}

export function findSelectedCatalogEntry(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
): SelectedCatalogEntry | null {
  if (!catalog || !selection) {
    return null;
  }
  if (selection.deploymentRef) {
    for (const provider of catalog.providers) {
      const model = provider.models.find((candidate) =>
        sameDeploymentRef(
          getModelDeploymentRef(candidate),
          selection.deploymentRef,
        ),
      );
      if (model) {
        return { provider, model };
      }
    }
    return null;
  }
  const provider = findProvider(catalog, selection.provider);
  const model = findModel(provider, selection.model);
  return provider && model ? { provider, model } : null;
}

export function getModelDeploymentRef(
  model: LLMCatalogModel,
): LLMDeploymentRef | null {
  return model.deploymentRef
    ?? model.connection?.deploymentRef
    ?? model.proving?.deploymentRef
    ?? null;
}

export function getSelectedModelDisplayLabel(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
  options: { includeProvider?: boolean } = {},
): string {
  if (!selection) {
    return "Select model";
  }
  const entry = findSelectedCatalogEntry(catalog, selection);
  if (!entry) {
    return selection.model;
  }
  return options.includeProvider
    ? `${entry.provider.label} / ${entry.model.label}`
    : entry.model.label;
}

export function getProviderDefaultSelection(
  catalog: LLMModelCatalogResponse | null | undefined,
  providerId: string | null | undefined,
): SelectedLLMModel | null {
  const provider = findProvider(catalog, providerId);
  if (!provider || !provider.defaultModel || !findModel(provider, provider.defaultModel)) {
    return null;
  }
  return {
    provider: provider.id,
    model: provider.defaultModel,
  };
}

export function getFirstCatalogDefaultSelection(
  catalog: LLMModelCatalogResponse | null | undefined,
): SelectedLLMModel | null {
  if (!catalog) {
    return null;
  }
  for (const provider of catalog.providers) {
    if (!provider.available || !provider.selectable) {
      continue;
    }
    const selection = getProviderDefaultSelection(catalog, provider.id);
    if (selection) {
      return selection;
    }
  }
  return null;
}

export function isSelectionSelectable(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
): boolean {
  const entry = findSelectedCatalogEntry(catalog, selection);
  return Boolean(entry?.provider.available && entry.provider.selectable);
}

export function getBlockingSelectionStatus(
  savedSelection: LLMSelection | null | undefined,
  selectedSelection: SelectedLLMModel | null | undefined,
): LLMSelectionStatus | null {
  if (!savedSelection?.selectionStatus || !selectedSelection) {
    return null;
  }
  if (
    savedSelection.provider !== selectedSelection.provider
    || savedSelection.model !== selectedSelection.model
  ) {
    return null;
  }
  return savedSelection.selectionStatus.runnable === false
    ? savedSelection.selectionStatus
    : null;
}

export function sameDeploymentRef(
  left: LLMDeploymentRef | null | undefined,
  right: LLMDeploymentRef | null | undefined,
): boolean {
  return Boolean(left && right && left.deployment_id === right.deployment_id);
}
