/**
 * Temporary frontend visibility policy for incomplete self-hosted LLM routes.
 *
 * Ollama and vLLM remain hidden by default because their current user-entered
 * endpoint flow cannot safely register operator-approved loopback or private
 * network targets. Keep this gate default-off until an operator-controlled
 * self-hosted model registration system replaces that unfinished flow.
 */

import { isIncompleteSelfHostedLLMSettingsEnabled } from "@/config/feature-flags";

const INCOMPLETE_SELF_HOSTED_PROVIDER_IDS = new Set([
  "ollama_openai_compatible_chat",
  "vllm_openai_compatible_chat",
]);

/** Hide incomplete provider routes unless the temporary build-time gate is enabled. */
export function isIncompleteSelfHostedProviderVisible(providerId: string): boolean {
  return !INCOMPLETE_SELF_HOSTED_PROVIDER_IDS.has(providerId)
    || isIncompleteSelfHostedLLMSettingsEnabled();
}
