/**
 * Provider-first chat model picker backed by the public LLM catalog.
 *
 * Owns provider/model menu rendering and capability-aware reasoning effort
 * choices without duplicating provider-specific model policy in the frontend.
 */
import { Check, ChevronDown } from "lucide-react";

import {
  findSelectedCatalogEntry,
  getSelectedModelDisplayLabel,
} from "@/features/llm-provider/catalog";
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

function isSameSelection(
  left: SelectedLLMModel | null | undefined,
  right: SelectedLLMModel,
): boolean {
  return left?.provider === right.provider && left?.model === right.model;
}

function isModelSelectable(provider: LLMCatalogProvider): boolean {
  return provider.available && provider.selectable;
}

function ModelRowContent({
  model,
  selected,
}: {
  model: LLMCatalogModel;
  selected: boolean;
}) {
  return (
    <div className="flex w-full min-w-0 items-center justify-between gap-2">
      <span className="truncate">{model.label}</span>
      {selected ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : null}
    </div>
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
  const selectedEntry = findSelectedCatalogEntry(catalog, selectedSelection);
  const selectedLabel = getSelectedModelDisplayLabel(
    catalog,
    selectedSelection,
    { includeProvider: true },
  );
  const showEffortBadge = modelSupportsReasoningEffort(selectedEntry?.model);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={providers.length === 0}>
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
            {providers.length === 0 ? "No models available" : selectedLabel}
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
        {providers.map((provider) => {
          const providerSelectable = isModelSelectable(provider);
          const providerSelected = selectedSelection?.provider === provider.id;
          return (
            <DropdownMenuSub key={provider.id}>
              <DropdownMenuSubTrigger className="cursor-pointer text-xs [&>svg:last-child]:hidden">
                <div className="flex w-full min-w-0 items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-col">
                    <span className="truncate text-slate-200">{provider.label}</span>
                    {!providerSelectable ? (
                      <span className="truncate text-[10px] text-slate-500">Unavailable</span>
                    ) : null}
                  </div>
                  {providerSelected ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : null}
                </div>
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="min-w-[240px] !overflow-visible">
                {provider.models.length === 0 ? (
                  <DropdownMenuItem disabled className="text-xs">
                    No models available
                  </DropdownMenuItem>
                ) : (
                  provider.models.map((model) => {
                    const selection = { provider: provider.id, model: model.id };
                    const selected = isSameSelection(selectedSelection, selection);
                    const effortOptions = getVisibleReasoningEffortOptions(model);
                    const modelSelectable = providerSelectable;

                    if (effortOptions.length > 0) {
                      return (
                        <DropdownMenuSub key={model.id}>
                          <DropdownMenuSubTrigger
                            className="cursor-pointer text-xs [&>svg:last-child]:hidden"
                            onClick={(event) => {
                              if (!modelSelectable) {
                                event.preventDefault();
                                return;
                              }
                              event.preventDefault();
                              onModelChange(selection);
                            }}
                          >
                            <ModelRowContent model={model} selected={selected} />
                          </DropdownMenuSubTrigger>
                          <DropdownMenuSubContent className="min-w-[160px]">
                            {effortOptions.map((effort) => (
                              <DropdownMenuItem
                                key={effort}
                                disabled={!modelSelectable}
                                className="cursor-pointer text-xs capitalize"
                                onSelect={() => {
                                  onModelChange(selection, { reasoningEffort: effort });
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
                        key={model.id}
                        disabled={!modelSelectable}
                        className="cursor-pointer text-xs"
                        onSelect={() => onModelChange(selection)}
                      >
                        <ModelRowContent model={model} selected={selected} />
                      </DropdownMenuItem>
                    );
                  })
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
