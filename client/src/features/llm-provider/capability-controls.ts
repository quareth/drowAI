/**
 * Capability gates for provider-neutral LLM controls.
 *
 * UI and runtime call sites should use these helpers instead of model-name
 * checks when deciding which controls or payload fields are valid.
 */

import { findSelectedCatalogEntry } from "./catalog";
import type {
  LLMCatalogModel,
  LLMModelCatalogResponse,
  SelectedLLMModel,
  VisibleLLMReasoningEffort,
} from "./types";

const OPENAI_PROVIDER_ID = "openai";

export const LLM_CAPABILITIES = {
  chat: "chat",
  streaming: "streaming",
  tools: "tools",
  parallelTools: "parallel_tools",
  structuredOutputNative: "structured_output_native",
  structuredOutputToolFallback: "structured_output_tool_fallback",
  reasoningEffort: "reasoning_effort",
} as const;

export function modelHasCapability(
  model: LLMCatalogModel | null | undefined,
  capability: string,
): boolean {
  return Array.isArray(model?.capabilities) && model.capabilities.includes(capability);
}

export function supportsReasoningEffort(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
): boolean {
  const entry = findSelectedCatalogEntry(catalog, selection);
  return modelSupportsReasoningEffort(entry?.model);
}

export function modelSupportsReasoningEffort(
  model: LLMCatalogModel | null | undefined,
): boolean {
  return modelHasCapability(model, LLM_CAPABILITIES.reasoningEffort)
    && Array.isArray(model?.reasoningEfforts)
    && model.reasoningEfforts.length > 0;
}

export function getVisibleReasoningEffortOptions(
  model: LLMCatalogModel | null | undefined,
): readonly VisibleLLMReasoningEffort[] {
  if (!modelSupportsReasoningEffort(model)) {
    return [];
  }
  return model?.visibleReasoningEfforts ?? [];
}

export function getDefaultVisibleReasoningEffort(
  model: LLMCatalogModel | null | undefined,
): VisibleLLMReasoningEffort | null {
  const options = getVisibleReasoningEffortOptions(model);
  if (model?.defaultVisibleReasoningEffort && options.includes(model.defaultVisibleReasoningEffort)) {
    return model.defaultVisibleReasoningEffort;
  }
  return options[0] ?? null;
}

export function shouldOmitReasoningEffort(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
  effort: string | null | undefined,
): boolean {
  if (!effort) {
    return true;
  }
  const entry = findSelectedCatalogEntry(catalog, selection);
  const model = entry?.model;
  return !modelSupportsReasoningEffort(model) || !model?.reasoningEfforts.includes(effort);
}

export function getSupportedReasoningEffortForPayload(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
  effort: VisibleLLMReasoningEffort | null | undefined,
): VisibleLLMReasoningEffort | undefined {
  if (!effort || !selection) {
    return undefined;
  }
  if (!catalog) {
    return selection.provider === OPENAI_PROVIDER_ID ? effort : undefined;
  }
  if (shouldOmitReasoningEffort(catalog, selection, effort)) {
    return undefined;
  }
  return effort;
}

export function supportsTools(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
): boolean {
  const entry = findSelectedCatalogEntry(catalog, selection);
  return modelHasCapability(entry?.model, LLM_CAPABILITIES.tools);
}

export function supportsToolChoiceMode(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
  mode: string,
): boolean {
  const entry = findSelectedCatalogEntry(catalog, selection);
  return Boolean(entry?.model.toolChoiceModes?.includes(mode));
}

export function supportsStructuredOutputStrategy(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
  strategy: string,
): boolean {
  const entry = findSelectedCatalogEntry(catalog, selection);
  return Boolean(entry?.model.structuredOutputStrategies?.includes(strategy));
}

export function supportsNativeStructuredOutput(
  catalog: LLMModelCatalogResponse | null | undefined,
  selection: SelectedLLMModel | null | undefined,
): boolean {
  const entry = findSelectedCatalogEntry(catalog, selection);
  return modelHasCapability(entry?.model, LLM_CAPABILITIES.structuredOutputNative)
    || Boolean(entry?.model.structuredOutputStrategies?.includes("native_schema"));
}
