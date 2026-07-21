/**
 * Frontend feature-flag and environment-gate configuration.
 *
 * Feature gates are controlled by explicit Vite build environment variables.
 * Sensitive incomplete-feature gates must stay env-only and default-off.
 */

export interface FeatureFlags {
  enableOptimisticUpdates: boolean;
  enableBasicChat: boolean;
  enableSendQueueUI: boolean;
  // Toggles for streaming-state and chat-filter behavior.
  enableUnifiedStreamingState: boolean;
  enableUnifiedChatFilters: boolean;
  // Rollout gate for multiplex full-packet websocket streaming.
  enableMultiTaskStreamManager: boolean;
}

const DEFAULT_FLAGS: FeatureFlags = {
  enableOptimisticUpdates: true,
  enableBasicChat: true,
  enableSendQueueUI: true,
  enableUnifiedStreamingState: true,
  enableUnifiedChatFilters: true,
  enableMultiTaskStreamManager: true,
};

const parseEnvFlag = (value: unknown): boolean | undefined => {
  if (typeof value === 'string') {
    return value.toLowerCase() === 'true';
  }
  return undefined;
};

const envOverrides: Partial<FeatureFlags> = {};

const envEnableOptimistic = parseEnvFlag(import.meta.env.VITE_ENABLE_OPTIMISTIC_UPDATES);
if (typeof envEnableOptimistic === 'boolean') {
  envOverrides.enableOptimisticUpdates = envEnableOptimistic;
}

const envEnableBasicChat = parseEnvFlag(import.meta.env.VITE_ENABLE_BASIC_CHAT);
if (typeof envEnableBasicChat === 'boolean') {
  envOverrides.enableBasicChat = envEnableBasicChat;
}

// Toggle queued-send UI.
const envEnableSendQueueUI = parseEnvFlag(import.meta.env.VITE_ENABLE_SEND_QUEUE_UI);
if (typeof envEnableSendQueueUI === 'boolean') {
  envOverrides.enableSendQueueUI = envEnableSendQueueUI;
}

// Toggles for unified streaming state and chat filters.
const envEnableUnifiedStreaming = parseEnvFlag(import.meta.env.VITE_ENABLE_UNIFIED_STREAMING_STATE);
if (typeof envEnableUnifiedStreaming === 'boolean') {
  envOverrides.enableUnifiedStreamingState = envEnableUnifiedStreaming;
}
const envEnableUnifiedChatFilters = parseEnvFlag(import.meta.env.VITE_ENABLE_UNIFIED_CHAT_FILTERS);
if (typeof envEnableUnifiedChatFilters === 'boolean') {
  envOverrides.enableUnifiedChatFilters = envEnableUnifiedChatFilters;
}

const envEnableMultiTaskStreamManager = parseEnvFlag(import.meta.env.VITE_ENABLE_MULTI_TASK_STREAM_MANAGER);
if (typeof envEnableMultiTaskStreamManager === 'boolean') {
  envOverrides.enableMultiTaskStreamManager = envEnableMultiTaskStreamManager;
}

export const featureFlags: FeatureFlags = {
  ...DEFAULT_FLAGS,
  ...envOverrides,
};

/**
 * Gate unfinished Ollama and vLLM UI behind an explicit internal build flag.
 * Keep this env-only and default-off until operator-controlled self-hosted
 * model registration supports approved loopback and private-network targets.
 */
export function isIncompleteSelfHostedLLMSettingsEnabled(): boolean {
  return parseEnvFlag(import.meta.env.VITE_ENABLE_INCOMPLETE_SELF_HOSTED_LLM_SETTINGS) === true;
}
