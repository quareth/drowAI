/**
 * Verifies the temporary default-off visibility policy for unfinished
 * self-hosted LLM provider registration.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { isIncompleteSelfHostedProviderVisible } from "../self-hosted-visibility";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("self-hosted LLM visibility", () => {
  it("hides Ollama and vLLM by default without affecting completed providers", () => {
    expect(isIncompleteSelfHostedProviderVisible("ollama_openai_compatible_chat")).toBe(false);
    expect(isIncompleteSelfHostedProviderVisible("vllm_openai_compatible_chat")).toBe(false);
    expect(isIncompleteSelfHostedProviderVisible("nvidia_nim_openai_compatible_chat")).toBe(true);
  });

  it("exposes both incomplete providers only when the internal gate is enabled", () => {
    vi.stubEnv("VITE_ENABLE_INCOMPLETE_SELF_HOSTED_LLM_SETTINGS", "true");

    expect(isIncompleteSelfHostedProviderVisible("ollama_openai_compatible_chat")).toBe(true);
    expect(isIncompleteSelfHostedProviderVisible("vllm_openai_compatible_chat")).toBe(true);
  });
});
