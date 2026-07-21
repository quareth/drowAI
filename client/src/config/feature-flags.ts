/**
 * Frontend feature-flag and environment-gate configuration.
 *
 * General rollout flags may accept browser-local overrides. Sensitive
 * incomplete-feature gates must read only their explicit build environment
 * variable so users cannot expose unfinished UI through local storage.
 */

export interface FeatureFlags {
  enableOptimisticUpdates: boolean;
  enableThinkingExpansion: boolean;
  enableBasicChat: boolean;
  useConversationAPIForGpt5: boolean;
  enableSendQueueUI: boolean;
  // Toggles for streaming-state and chat-filter behavior.
  enableUnifiedStreamingState: boolean;
  enableUnifiedChatFilters: boolean;
  // Rollout gate for multiplex full-packet websocket streaming.
  enableMultiTaskStreamManager: boolean;
}

const DEFAULT_FLAGS: FeatureFlags = {
  enableOptimisticUpdates: true,
  enableThinkingExpansion: true,
  enableBasicChat: true,
  useConversationAPIForGpt5: true,
  enableSendQueueUI: true,
  enableUnifiedStreamingState: true,
  enableUnifiedChatFilters: true,
  enableMultiTaskStreamManager: true,
};

const processEnv = typeof process !== 'undefined' ? process.env : undefined;
const importMetaEnv = typeof import.meta !== 'undefined' ? (import.meta as any).env : undefined;

const readEnvFlag = (...keys: string[]): boolean | undefined => {
  for (const key of keys) {
    const value =
      (importMetaEnv && typeof importMetaEnv[key] === 'string' ? importMetaEnv[key] : undefined) ??
      (processEnv && typeof processEnv[key] === 'string' ? processEnv[key] : undefined);
    if (typeof value === 'string') {
      return value.toLowerCase() === 'true';
    }
  }
  return undefined;
};

const clampPercent = (value: number): number => {
  const normalized = Math.floor(value);
  if (normalized < 0) {
    return 0;
  }
  if (normalized > 100) {
    return 100;
  }
  return normalized;
};

const envOverrides: Partial<FeatureFlags> = {};

const envEnableOptimistic = readEnvFlag('VITE_ENABLE_OPTIMISTIC_UPDATES', 'REACT_APP_ENABLE_OPTIMISTIC_UPDATES');
if (typeof envEnableOptimistic === 'boolean') {
  envOverrides.enableOptimisticUpdates = envEnableOptimistic;
}

const envEnableThinking = readEnvFlag('VITE_ENABLE_THINKING_EXPANSION', 'REACT_APP_ENABLE_THINKING_EXPANSION');
if (typeof envEnableThinking === 'boolean') {
  envOverrides.enableThinkingExpansion = envEnableThinking;
}

const envEnableBasicChat = readEnvFlag('VITE_ENABLE_BASIC_CHAT', 'REACT_APP_ENABLE_BASIC_CHAT');
if (typeof envEnableBasicChat === 'boolean') {
  envOverrides.enableBasicChat = envEnableBasicChat;
}

const envUseConversationAPI = readEnvFlag('VITE_USE_CONVERSATION_API_FOR_GPT5', 'REACT_APP_USE_CONVERSATION_API_FOR_GPT5');
if (typeof envUseConversationAPI === 'boolean') {
  envOverrides.useConversationAPIForGpt5 = envUseConversationAPI;
}

// Toggle queued-send UI.
const envEnableSendQueueUI = readEnvFlag('VITE_ENABLE_SEND_QUEUE_UI', 'REACT_APP_ENABLE_SEND_QUEUE_UI');
if (typeof envEnableSendQueueUI === 'boolean') {
  envOverrides.enableSendQueueUI = envEnableSendQueueUI;
}

// Toggles for unified streaming state and chat filters.
const envEnableUnifiedStreaming = readEnvFlag('VITE_ENABLE_UNIFIED_STREAMING_STATE', 'REACT_APP_ENABLE_UNIFIED_STREAMING_STATE');
if (typeof envEnableUnifiedStreaming === 'boolean') {
  envOverrides.enableUnifiedStreamingState = envEnableUnifiedStreaming;
}
const envEnableUnifiedChatFilters = readEnvFlag('VITE_ENABLE_UNIFIED_CHAT_FILTERS', 'REACT_APP_ENABLE_UNIFIED_CHAT_FILTERS');
if (typeof envEnableUnifiedChatFilters === 'boolean') {
  envOverrides.enableUnifiedChatFilters = envEnableUnifiedChatFilters;
}

const envEnableMultiplexFullPacketStream = readEnvFlag(
  'VITE_ENABLE_MULTIPLEX_FULL_PACKET_STREAM',
  'REACT_APP_ENABLE_MULTIPLEX_FULL_PACKET_STREAM',
  'VITE_ENABLE_MULTI_TASK_STREAM_MANAGER',
  'REACT_APP_ENABLE_MULTI_TASK_STREAM_MANAGER',
);
if (typeof envEnableMultiplexFullPacketStream === 'boolean') {
  envOverrides.enableMultiTaskStreamManager = envEnableMultiplexFullPacketStream;
}

const storageRef =
  typeof window !== "undefined" &&
  window.localStorage &&
  typeof window.localStorage.getItem === "function"
    ? window.localStorage
    : null;
const localOverridesRaw = storageRef ? storageRef.getItem("featureFlags") : null;
let overrides: Partial<FeatureFlags> = {};

if (localOverridesRaw) {
  try {
    overrides = JSON.parse(localOverridesRaw) as Partial<FeatureFlags>;
  } catch {
    overrides = {};
  }
}

export const featureFlags: FeatureFlags = {
  ...DEFAULT_FLAGS,
  ...envOverrides,
  ...overrides,
};

export function isFeatureEnabled<K extends keyof FeatureFlags>(flag: K): FeatureFlags[K] {
  return featureFlags[flag];
}

/**
 * Gate unfinished Ollama and vLLM UI behind an explicit internal build flag.
 * Keep this env-only and default-off until operator-controlled self-hosted
 * model registration supports approved loopback and private-network targets.
 */
export function isIncompleteSelfHostedLLMSettingsEnabled(): boolean {
  return readEnvFlag('VITE_ENABLE_INCOMPLETE_SELF_HOSTED_LLM_SETTINGS') === true;
}

export function isTaskInPercentageRollout(taskId: number | null | undefined, percent: number): boolean {
  if (typeof taskId !== 'number' || !Number.isFinite(taskId) || taskId <= 0) {
    return false;
  }
  const normalizedPercent = clampPercent(percent);
  if (normalizedPercent <= 0) {
    return false;
  }
  if (normalizedPercent >= 100) {
    return true;
  }
  return taskId % 100 < normalizedPercent;
}
