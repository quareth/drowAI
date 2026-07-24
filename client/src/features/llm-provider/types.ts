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

export type VisibleLLMReasoningEffort =
  | "none"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max";

export interface SelectedLLMModel {
  provider: ProviderId;
  model: ModelId;
  deploymentRef?: LLMDeploymentRef | null;
}

export interface LLMProviderCredentialStatus {
  user_id: number;
  provider: ProviderId;
  enabled: boolean;
  has_api_key: boolean;
  masked_api_key?: string | null;
  connection_ref?: LLMConnectionRef | null;
  auth_mode?: string | null;
}

export interface LLMConnectionRef {
  connection_id: string;
  expected_revision: number;
}

export interface LLMDeploymentRef {
  deployment_id: string;
  expected_revision: number;
}

export interface LLMProvingUsageEvidence {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface LLMProvingVerification {
  status: string;
  code: string;
  message: string;
  retryable: boolean;
  observedAt?: string | null;
  expiresAt?: string | null;
  modelPresent?: boolean | null;
  usage?: LLMProvingUsageEvidence | null;
}

export interface LLMProvingConnectionStatus {
  lifecycleState: string;
  connectionRef?: LLMConnectionRef | null;
  deploymentRef?: LLMDeploymentRef | null;
  verification?: LLMProvingVerification | null;
  runnability?: LLMSelectionStatus | null;
}

export interface LLMProvingMetadata extends LLMProvingConnectionStatus {
  presetId: string;
  displayName: string;
  enabled: boolean;
  authMode: string;
  userConfigFields: string[];
  configFields?: LLMConnectionConfigField[];
}

export interface LLMConnectionConfigField {
  name: string;
  label: string;
  fieldType: "text" | "password" | "url" | (string & {});
  required: boolean;
  secret: boolean;
}

export interface LLMConnectionMetadata extends LLMProvingConnectionStatus {
  presetId: string;
  displayName: string;
  enabled: boolean;
  authMode: string;
  userConfigFields: string[];
  configFields: LLMConnectionConfigField[];
}

export interface LLMCatalogModel {
  id: ModelId;
  canonicalModelId?: string;
  exactWireModelId?: string | null;
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
  deploymentRef?: LLMDeploymentRef | null;
  runnable?: boolean;
  connection?: LLMConnectionMetadata | null;
  proving?: LLMProvingMetadata | null;
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
  deployment_ref?: LLMDeploymentRef | null;
}

export interface LLMSelection extends SelectedLLMModel {
  selectionStatus?: LLMSelectionStatus;
  deploymentRef?: LLMDeploymentRef | null;
}

export interface LLMDeploymentSelection {
  deployment_ref: LLMDeploymentRef;
}

export interface ReportingLLMSelectionApiResponse {
  provider: ProviderId | null;
  model: ModelId | null;
  reasoning_effort?: LLMReasoningEffort | null;
  selection_status: LLMSelectionStatus;
  deployment_ref?: LLMDeploymentRef | null;
}

export interface ReportingLLMSelection {
  provider: ProviderId | null;
  model: ModelId | null;
  reasoningEffort?: LLMReasoningEffort | null;
  selectionStatus: LLMSelectionStatus;
  deploymentRef?: LLMDeploymentRef | null;
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

export interface LLMManagedConnectionSaveRequest {
  connection_ref?: LLMConnectionRef | null;
  display_label?: string | null;
  api_key?: string | null;
  base_url?: string | null;
  wire_model_id?: string | null;
  model_label?: string | null;
  canonical_model_id?: string | null;
}

export interface LLMManagedConnectionTestRequest {
  api_key?: string | null;
  connection_ref?: LLMConnectionRef | null;
}

export interface LLMManagedConnectionRefreshRequest {
  api_key?: string | null;
  connection_ref: LLMConnectionRef;
}

export interface LLMManagedConnectionEnableRequest {
  connection_ref: LLMConnectionRef;
  deployment_ref?: LLMDeploymentRef | null;
}

export interface LLMManagedConnectionDisconnectRequest {
  connection_ref: LLMConnectionRef;
}
