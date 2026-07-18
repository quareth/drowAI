/**
 * Frontend contracts for provider-neutral LLM catalog and selection APIs.
 *
 * This module mirrors the public backend response shape and owns small value
 * types shared by LLM provider UI, chat selection, and runtime switch calls.
 */

export type ProviderId = string;
export type ModelId = string;

export type LLMReasoningEffort =
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max"
  | (string & {});

export type VisibleLLMReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max";

export interface SelectedLLMModel {
  provider: ProviderId;
  model: ModelId;
}

export interface LLMProviderCredentialStatus {
  user_id: number;
  provider: ProviderId;
  enabled: boolean;
  has_api_key: boolean;
  masked_api_key?: string | null;
}

export interface LLMCatalogModel {
  id: ModelId;
  label: string;
  apiSurface: string;
  capabilities: string[];
  contextWindowTokens: number;
  maxOutputTokens: number;
  reasoningEfforts: LLMReasoningEffort[];
  visibleReasoningEfforts: VisibleLLMReasoningEffort[];
  defaultReasoningEffort?: LLMReasoningEffort | null;
  defaultVisibleReasoningEffort?: VisibleLLMReasoningEffort | null;
  toolChoiceModes: string[];
  structuredOutputStrategies: string[];
  pricingStatus?: string;
}

export interface LLMCatalogProvider {
  id: ProviderId;
  label: string;
  capabilities: string[];
  available: boolean;
  selectable: boolean;
  credential: LLMProviderCredentialStatus;
  models: LLMCatalogModel[];
  defaultModel: ModelId;
}

export interface LLMModelCatalogResponse {
  providers: LLMCatalogProvider[];
}

export type LLMSelectionStatusCode =
  | "selectable"
  | "credential_missing"
  | "adapter_unavailable"
  | "model_unavailable"
  | "invalid_selection"
  | (string & {});

export interface LLMSelectionStatus {
  status: LLMSelectionStatusCode;
  selectable: boolean;
  runnable: boolean;
  reason?: string | null;
}

export interface LLMSelectionApiResponse extends SelectedLLMModel {
  selection_status?: LLMSelectionStatus;
}

export interface LLMSelection extends SelectedLLMModel {
  selectionStatus?: LLMSelectionStatus;
}

export interface ReportingLLMSelectionApiResponse {
  provider: ProviderId | null;
  model: ModelId | null;
  reasoning_effort?: LLMReasoningEffort | null;
  selection_status: LLMSelectionStatus;
}

export interface ReportingLLMSelection {
  provider: ProviderId | null;
  model: ModelId | null;
  reasoningEffort?: LLMReasoningEffort | null;
  selectionStatus: LLMSelectionStatus;
}

export interface ReportingLLMSelectionUpsert extends SelectedLLMModel {
  reasoning_effort?: LLMReasoningEffort | null;
}

export interface LLMProviderCredentialUpsert {
  api_key: string;
  enabled?: boolean;
}

export interface LLMProviderCredentialTestRequest {
  api_key?: string | null;
}

export interface LLMProviderCredentialTestResponse {
  provider: ProviderId;
  status: string;
  message: string;
  model_count?: number | null;
}

export interface LLMProviderCredentialDeleteResponse {
  success: boolean;
}
