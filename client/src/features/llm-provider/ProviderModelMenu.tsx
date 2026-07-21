/**
 * Product-grouped chat model picker backed by the public LLM catalog.
 *
 * Owns model/deployment menu rendering and capability-aware reasoning effort
 * choices without duplicating provider-specific model policy in the frontend.
 */
import { useMemo } from "react";
import { Check, ChevronDown } from "lucide-react";

import { findSelectedCatalogEntry } from "@/features/llm-provider/catalog";
import {
  getVisibleReasoningEffortOptions,
  modelSupportsReasoningEffort,
} from "@/features/llm-provider/capability-controls";
import type {
  LLMCatalogModel,
  LLMCatalogProvider,
  LLMModelCatalogResponse,
  SelectedLLMModel,
  VisibleLLMReasoningEffort,
} from "@/features/llm-provider/types";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export interface ProviderModelMenuProps {
  catalog: LLMModelCatalogResponse | undefined;
  selectedSelection: SelectedLLMModel | null;
  selectedReasoningEffort: VisibleLLMReasoningEffort;
  onModelChange: (
    selection: SelectedLLMModel,
    options?: { reasoningEffort?: VisibleLLMReasoningEffort },
  ) => void;
  className?: string;
}

interface ModelChoice {
  key: string;
  label: string;
  providerLabel: string;
  selection: SelectedLLMModel;
  model: LLMCatalogModel;
  selectable: boolean;
  statusLabel: string | null;
}

interface ModelGroup {
  key: string;
  label: string;
  choices: ModelChoice[];
}

interface PublisherGroup {
  key: string;
  label: string;
  models: ModelGroup[];
}

function isSameSelection(
  left: SelectedLLMModel | null | undefined,
  right: SelectedLLMModel,
): boolean {
  const sameLegacy = left?.provider === right.provider && left?.model === right.model;
  if (!sameLegacy) {
    return false;
  }
  if (left?.deploymentRef || right.deploymentRef) {
    return left?.deploymentRef?.deployment_id === right.deploymentRef?.deployment_id;
  }
  return true;
}

function isModelSelectable(provider: LLMCatalogProvider): boolean {
  return provider.available && provider.selectable;
}

function ModelRowContent({
  label,
  selected,
  statusLabel,
}: {
  label: string;
  selected: boolean;
  statusLabel?: string | null;
}) {
  return (
    <div className="flex w-full min-w-0 items-center justify-between gap-2">
      <div className="flex min-w-0 flex-col">
        <span className="truncate">{label}</span>
        {statusLabel ? (
          <span className="truncate text-[10px] text-slate-500">{statusLabel}</span>
        ) : null}
      </div>
      {selected ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : null}
    </div>
  );
}

function buildPublisherGroups(providers: LLMCatalogProvider[]): PublisherGroup[] {
  const publisherGroups = new Map<string, PublisherGroup>();
  const providerLabels = new Map(
    providers.map((provider) => [provider.id.toLowerCase(), provider.label]),
  );

  for (const provider of providers) {
    const providerSelectable = isModelSelectable(provider);
    for (const model of provider.models) {
      const deploymentRef =
        model.deploymentRef ??
        model.connection?.deploymentRef ??
        model.proving?.deploymentRef ??
        null;
      if (model.connection && !deploymentRef) {
        continue;
      }
      const deploymentBacked = Boolean(model.connection || model.proving || model.deploymentRef);
      const runnable =
        model.connection?.runnability?.runnable ??
        model.proving?.runnability?.runnable ??
        model.runnable ??
        false;
      if (deploymentBacked && !runnable) {
        continue;
      }
      const legacyCredentialReady =
        provider.credential.enabled && provider.credential.has_api_key;
      const selectable =
        providerSelectable &&
        (deploymentBacked
          ? Boolean(deploymentRef && runnable)
          : legacyCredentialReady);
      const publisherKey = canonicalPublisherKey(provider, model);
      const modelKey = model.canonicalModelId?.trim() || `${provider.id}:${model.id}`;
      const groupLabel = canonicalModelLabel(model);
      const statusLabel = selectable
        ? null
        : !providerSelectable
          ? "Unavailable"
          : !deploymentBacked && !legacyCredentialReady
            ? "Configure credentials"
            : "Not ready";
      const choice: ModelChoice = {
        key: `${provider.id}:${model.id}:${deploymentRef?.deployment_id ?? "legacy"}`,
        label: deploymentRef ? deploymentChoiceLabel(provider, model) : model.label,
        providerLabel: provider.label,
        selection: {
          provider: provider.id,
          model: model.id,
          ...(deploymentRef ? { deploymentRef } : {}),
        },
        model,
        selectable,
        statusLabel,
      };

      const publisher = publisherGroups.get(publisherKey);
      if (publisher) {
        appendModelChoice(publisher.models, modelKey, groupLabel, choice);
      } else {
        publisherGroups.set(publisherKey, {
          key: publisherKey,
          label: canonicalPublisherLabel(publisherKey, providerLabels),
          models: [{
            key: modelKey,
            label: groupLabel,
            choices: [choice],
          }],
        });
      }
    }
  }

  return Array.from(publisherGroups.values()).map((publisher) => ({
    ...publisher,
    models: publisher.models.sort((left, right) => left.label.localeCompare(right.label)),
  }));
}

function appendModelChoice(
  models: ModelGroup[],
  modelKey: string,
  groupLabel: string,
  choice: ModelChoice,
) {
  const existing = models.find((model) => model.key === modelKey);
  if (existing) {
    existing.choices.push(choice);
    return;
  }
  models.push({
    key: modelKey,
    label: groupLabel,
    choices: [choice],
  });
}

function canonicalPublisherKey(
  provider: LLMCatalogProvider,
  model: LLMCatalogModel,
): string {
  if (model.canonicalModelId?.trim().toLowerCase() === "openai/gpt-oss-20b") {
    return "open_models";
  }
  return splitCanonicalModelId(model.canonicalModelId)?.publisher ?? provider.id;
}

function canonicalPublisherLabel(
  publisherKey: string,
  providerLabels: Map<string, string>,
): string {
  const knownLabel = providerLabels.get(publisherKey.toLowerCase());
  if (knownLabel) {
    return knownLabel;
  }
  const knownPublisherLabels: Record<string, string> = {
    anthropic: "Anthropic",
    open_models: "Open models",
    openai: "OpenAI",
  };
  return knownPublisherLabels[publisherKey.toLowerCase()] ?? titleCaseIdentifier(publisherKey);
}

function splitCanonicalModelId(
  canonicalModelId: string | null | undefined,
): { publisher: string; model: string } | null {
  const normalized = canonicalModelId?.trim();
  if (!normalized) {
    return null;
  }
  const slashIndex = normalized.indexOf("/");
  const colonIndex = normalized.indexOf(":");
  const separatorIndex =
    slashIndex >= 0 && colonIndex >= 0
      ? Math.min(slashIndex, colonIndex)
      : Math.max(slashIndex, colonIndex);
  if (separatorIndex <= 0) {
    return null;
  }
  return {
    publisher: normalized.slice(0, separatorIndex),
    model: normalized.slice(separatorIndex + 1),
  };
}

function titleCaseIdentifier(identifier: string): string {
  return identifier
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function canonicalModelLabel(model: LLMCatalogModel): string {
  return stripDeploymentQualifier(model.label.trim() || model.id);
}

function stripDeploymentQualifier(label: string): string {
  const stripped = label.replace(/\s+via\s+.+$/i, "").trim();
  return stripped || label;
}

function deploymentChoiceLabel(
  provider: LLMCatalogProvider,
  model: LLMCatalogModel,
): string {
  if (model.proving?.enabled) {
    return "GPT-OSS proving";
  }
  const productProviderLabels: Record<string, string> = {
    huggingface_openai_compatible_chat: "Hugging Face",
    nvidia_nim_openai_compatible_chat: "NVIDIA",
    ollama_openai_compatible_chat: "Ollama",
    vllm_openai_compatible_chat: "vLLM",
  };
  const providerLabel = productProviderLabels[provider.id] ?? provider.label;
  return model.canonicalModelId?.trim().toLowerCase() === "openai/gpt-oss-20b"
    ? `Run with ${providerLabel}`
    : providerLabel;
}

function selectedModelDisplayLabel(
  publishers: PublisherGroup[],
  selectedSelection: SelectedLLMModel | null,
): string {
  if (!selectedSelection) {
    return "Select model";
  }
  for (const publisher of publishers) {
    for (const group of publisher.models) {
      const selectedChoice = group.choices.find((choice) =>
        isSameSelection(selectedSelection, choice.selection),
      );
      if (selectedChoice) {
        return group.choices.length > 1
          ? `${group.label} / ${selectedChoice.label}`
          : group.label;
      }
    }
  }
  return selectedSelection.model;
}

function renderModelGroup(
  group: ModelGroup,
  selectedSelection: SelectedLLMModel | null,
  selectedReasoningEffort: VisibleLLMReasoningEffort,
  onModelChange: ProviderModelMenuProps["onModelChange"],
) {
  if (group.choices.length === 1) {
    return renderChoice(
      { ...group.choices[0], label: group.label },
      selectedSelection,
      selectedReasoningEffort,
      onModelChange,
    );
  }

  const selected = group.choices.some((choice) =>
    isSameSelection(selectedSelection, choice.selection),
  );
  return (
    <DropdownMenuSub key={group.key}>
      <DropdownMenuSubTrigger className="cursor-pointer text-xs [&>svg:last-child]:hidden">
        <ModelRowContent label={group.label} selected={selected} />
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent className="min-w-[220px] !overflow-visible">
        {group.choices.map((choice) =>
          renderChoice(
            choice,
            selectedSelection,
            selectedReasoningEffort,
            onModelChange,
          ),
        )}
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  );
}

function renderChoice(
  choice: ModelChoice,
  selectedSelection: SelectedLLMModel | null,
  selectedReasoningEffort: VisibleLLMReasoningEffort,
  onModelChange: ProviderModelMenuProps["onModelChange"],
) {
  const selected = isSameSelection(selectedSelection, choice.selection);
  const effortOptions = getVisibleReasoningEffortOptions(choice.model);

  if (effortOptions.length > 0) {
    return (
      <DropdownMenuSub key={choice.key}>
        <DropdownMenuSubTrigger
          className="cursor-pointer text-xs [&>svg:last-child]:hidden"
          onClick={(event) => {
            if (!choice.selectable) {
              event.preventDefault();
              return;
            }
            event.preventDefault();
            onModelChange(choice.selection);
          }}
        >
          <ModelRowContent
            label={choice.label}
            selected={selected}
            statusLabel={choice.statusLabel}
          />
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent className="min-w-[160px]">
          {effortOptions.map((effort) => (
            <DropdownMenuItem
              key={effort}
              disabled={!choice.selectable}
              className="cursor-pointer text-xs capitalize"
              onSelect={() => {
                onModelChange(choice.selection, { reasoningEffort: effort });
              }}
            >
              <div className="flex w-full items-center justify-between">
                <span>{effort}</span>
                {selected && selectedReasoningEffort === effort ? (
                  <Check className="h-3.5 w-3.5 text-emerald-400" />
                ) : null}
              </div>
            </DropdownMenuItem>
          ))}
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    );
  }

  return (
    <DropdownMenuItem
      key={choice.key}
      disabled={!choice.selectable}
      className="cursor-pointer text-xs"
      onSelect={() => onModelChange(choice.selection)}
    >
      <ModelRowContent
        label={choice.label}
        selected={selected}
        statusLabel={choice.statusLabel}
      />
    </DropdownMenuItem>
  );
}

export function ProviderModelMenu({
  catalog,
  selectedSelection,
  selectedReasoningEffort,
  onModelChange,
  className,
}: ProviderModelMenuProps) {
  const providers = catalog?.providers ?? [];
  const publishers = useMemo(() => buildPublisherGroups(providers), [providers]);
  const selectedEntry = findSelectedCatalogEntry(catalog, selectedSelection);
  const selectedLabel = selectedModelDisplayLabel(publishers, selectedSelection);
  const showEffortBadge = modelSupportsReasoningEffort(selectedEntry?.model);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={publishers.length === 0}>
        <button
          type="button"
          aria-label="Select model"
          className={cn(
            "flex h-7 min-w-[195px] items-center justify-between gap-2 rounded-lg",
            "border border-white/[0.08] bg-slate-800/60 px-2.5 py-1 text-xs text-slate-200",
            "backdrop-blur-sm transition-all duration-150 hover:bg-slate-800/80 hover:border-white/[0.12]",
            "disabled:cursor-not-allowed disabled:opacity-60",
            className,
          )}
        >
          <span className="truncate text-left">
            {publishers.length === 0 ? "No models available" : selectedLabel}
          </span>
          <span className="flex items-center gap-1">
            {showEffortBadge ? (
              <span className="rounded bg-white/[0.04] px-1 py-px text-[10px] uppercase text-slate-400">
                {selectedReasoningEffort}
              </span>
            ) : null}
            <ChevronDown className="h-3 w-3 text-slate-400" />
          </span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[260px]">
        {publishers.map((publisher) => {
          const selected = publisher.models.some((group) =>
            group.choices.some((choice) =>
              isSameSelection(selectedSelection, choice.selection),
            ),
          );
          return (
            <DropdownMenuSub key={publisher.key}>
              <DropdownMenuSubTrigger className="cursor-pointer text-xs [&>svg:last-child]:hidden">
                <ModelRowContent label={publisher.label} selected={selected} />
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="min-w-[220px] !overflow-visible">
                {publisher.models.map((group) =>
                  renderModelGroup(
                    group,
                    selectedSelection,
                    selectedReasoningEffort,
                    onModelChange,
                  ),
                )}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export default ProviderModelMenu;
