/**
 * Pure lookup helpers for provider-neutral LLM catalog data.
 *
 * These functions derive labels and selected model metadata from backend-owned
 * catalog fields without duplicating provider-specific model policy.
 */

import type {
  LLMCatalogModel,
  LLMCatalogProvider,
  LLMDeploymentCandidate,
  LLMDeploymentCandidateGroup,
  LLMDeploymentRef,
  LLMDeploymentStatusOverride,
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
  if (!selection) {
    return null;
  }
  const provider = findProvider(catalog, selection.provider);
  const model = findModel(provider, selection.model);
  return provider && model ? { provider, model } : null;
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

export function getDeploymentCandidates(
  catalog: LLMModelCatalogResponse | null | undefined,
  statusOverrides: LLMDeploymentStatusOverride[] = [],
): LLMDeploymentCandidate[] {
  if (!catalog) {
    return [];
  }

  return catalog.providers.flatMap((provider) =>
    provider.models.flatMap((model) => {
      const connection = model.connection ?? model.proving ?? null;
      const deploymentRef = model.deploymentRef ?? connection?.deploymentRef ?? null;
      if (!deploymentRef) {
        return [];
      }
      const override = statusOverrides.find((candidateOverride) =>
        sameDeploymentRef(candidateOverride.deploymentRef, deploymentRef),
      );
      const runnability = connection?.runnability ?? null;
      const runnable = override?.runnable ?? runnability?.runnable ?? model.runnable ?? false;
      const lifecycleState =
        override?.lifecycleState
        ?? connection?.lifecycleState
        ?? (runnable ? "enabled" : "unknown");

      return [{
        providerId: provider.id,
        providerLabel: provider.label,
        deploymentLabel: deploymentCandidateLabel(connection?.displayName, provider.label),
        modelId: model.id,
        modelLabel: model.label,
        canonicalModelId: model.canonicalModelId,
        exactWireModelId: model.exactWireModelId,
        apiSurface: model.apiSurface,
        capabilities: model.capabilities,
        contextWindowTokens: model.contextWindowTokens,
        maxOutputTokens: model.maxOutputTokens,
        pricingStatus: model.pricingStatus,
        deploymentRef,
        lifecycleState,
        runnable,
        status: override?.status ?? runnability?.status ?? (runnable ? "runnable" : "unknown"),
        reason: override?.reason ?? runnability?.reason ?? null,
      }];
    }),
  );
}

export function getDeploymentCandidateGroups(
  catalog: LLMModelCatalogResponse | null | undefined,
  statusOverrides: LLMDeploymentStatusOverride[] = [],
): LLMDeploymentCandidateGroup[] {
  const candidates = getDeploymentCandidates(catalog, statusOverrides);
  const groups = new Map<string, LLMDeploymentCandidateGroup>();
  for (const candidate of candidates) {
    const key = deploymentCandidateGroupKey(candidate);
    const existing = groups.get(key);
    if (existing) {
      existing.candidates.push(candidate);
      continue;
    }
    groups.set(key, {
      key,
      modelLabel: getCanonicalModelDisplayLabel(
        catalog,
        candidate.canonicalModelId,
        candidate.modelLabel,
      ),
      canonicalModelId: candidate.canonicalModelId,
      candidates: [candidate],
    });
  }
  return Array.from(groups.values());
}

function deploymentCandidateGroupKey(candidate: LLMDeploymentCandidate): string {
  const canonical = candidate.canonicalModelId?.trim();
  return canonical || `${candidate.providerId}:${candidate.modelId}`;
}

function deploymentCandidateLabel(
  connectionDisplayName: string | null | undefined,
  fallbackProviderLabel: string,
): string {
  const displayName = connectionDisplayName?.trim();
  return displayName || fallbackProviderLabel;
}

function getCanonicalModelDisplayLabel(
  catalog: LLMModelCatalogResponse | null | undefined,
  canonicalModelId: string | null | undefined,
  fallback: string,
): string {
  const canonical = canonicalModelId?.trim();
  if (!catalog || !canonical) {
    return fallback;
  }

  const matchingLabels = catalog.providers.flatMap((provider) =>
    provider.models
      .filter((model) => model.canonicalModelId === canonical)
      .map((model) => model.label.trim())
      .filter(Boolean),
  );
  if (matchingLabels.length === 0) {
    return fallback;
  }
  return stripDeploymentQualifier(
    matchingLabels.sort((left, right) => left.length - right.length)[0],
  );
}

function stripDeploymentQualifier(label: string): string {
  const canonicalLabel = label.replace(/\s+via\s+.+$/i, "").trim();
  return canonicalLabel || label;
}

export function sameDeploymentRef(
  left: LLMDeploymentRef | null | undefined,
  right: LLMDeploymentRef | null | undefined,
): boolean {
  return Boolean(left && right && left.deployment_id === right.deployment_id);
}

export function isDeploymentCandidateSelectable(
  candidate: LLMDeploymentCandidate,
): boolean {
  return candidate.runnable === true && candidate.lifecycleState === "enabled";
}

export function getSingleEligibleDeployment(
  candidates: LLMDeploymentCandidate[],
): LLMDeploymentCandidate | null {
  const eligible = candidates.filter(isDeploymentCandidateSelectable);
  return eligible.length === 1 ? eligible[0] : null;
}

export function formatPricingStatus(status: string | null | undefined): string {
  const normalized = status?.trim().toLowerCase();
  if (!normalized || normalized === "unknown" || normalized === "unavailable") {
    return "unavailable";
  }
  return status as string;
}
