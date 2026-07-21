/**
 * Characterizes frontend LLM API helper route and payload contracts.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteLLMProviderCredential,
  fetchLLMModelCatalog,
  fetchLLMProviderCredential,
  fetchLLMSelection,
  fetchReportingLLMSelection,
  saveLLMDeploymentSelection,
  saveLLMProviderCredential,
  saveLLMSelection,
  saveReportingLLMSelection,
  testLLMProviderCredential,
} from "../api";

const mocked = vi.hoisted(() => ({
  apiCall: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiCall: mocked.apiCall,
}));

beforeEach(() => {
  mocked.apiCall.mockReset();
});

describe("deployment baseline LLM API helpers", () => {
  it("centralizes catalog and selection route payloads in api.ts", async () => {
    mocked.apiCall
      .mockResolvedValueOnce({ providers: [] })
      .mockResolvedValueOnce({
        provider: "openai",
        model: "gpt-5.2",
        selection_status: { status: "selectable", selectable: true, runnable: true },
      })
      .mockResolvedValueOnce({ provider: "anthropic", model: "claude-sonnet-5" });

    await expect(fetchLLMModelCatalog()).resolves.toEqual({ providers: [] });
    await expect(fetchLLMSelection()).resolves.toEqual({
      provider: "openai",
      model: "gpt-5.2",
      selectionStatus: { status: "selectable", selectable: true, runnable: true },
    });
    await expect(
      saveLLMSelection({ provider: "anthropic", model: "claude-sonnet-5" }),
    ).resolves.toEqual({ provider: "anthropic", model: "claude-sonnet-5" });

    expect(mocked.apiCall).toHaveBeenNthCalledWith(1, "/api/llm/models");
    expect(mocked.apiCall).toHaveBeenNthCalledWith(2, "/api/llm/selection");
    expect(mocked.apiCall).toHaveBeenNthCalledWith(3, "/api/llm/selection", {
      method: "PUT",
      body: JSON.stringify({ provider: "anthropic", model: "claude-sonnet-5" }),
    });
  });

  it("preserves backend-provided catalog model pricing status metadata", async () => {
    const catalog = {
      providers: [
        {
          id: "openai",
          label: "OpenAI",
          capabilities: ["chat"],
          available: true,
          selectable: true,
          credential: {
            user_id: 1,
            provider: "openai",
            enabled: true,
            has_api_key: true,
          },
          models: [
            {
              id: "gpt-5.2",
              label: "GPT-5.2",
              apiSurface: "responses",
              capabilities: ["chat", "tools"],
              contextWindowTokens: 128000,
              maxOutputTokens: 8192,
              reasoningEfforts: ["low", "medium", "high"],
              visibleReasoningEfforts: ["low", "medium", "high"],
              defaultReasoningEffort: "medium",
              defaultVisibleReasoningEffort: "medium",
              toolChoiceModes: ["auto"],
              structuredOutputStrategies: ["json_schema"],
              pricingStatus: "estimated",
            },
          ],
          defaultModel: "gpt-5.2",
        },
      ],
    };

    mocked.apiCall.mockResolvedValueOnce(catalog);

    await expect(fetchLLMModelCatalog()).resolves.toEqual(catalog);
    expect(mocked.apiCall).toHaveBeenCalledWith("/api/llm/models");
  });

  it("centralizes credential and reporting selection route payloads in api.ts", async () => {
    mocked.apiCall
      .mockResolvedValueOnce({
        user_id: 1,
        provider: "openai",
        enabled: true,
        has_api_key: true,
      })
      .mockResolvedValueOnce({
        user_id: 1,
        provider: "openai",
        enabled: true,
        has_api_key: true,
      })
      .mockResolvedValueOnce({ provider: "openai", status: "success", message: "ok" })
      .mockResolvedValueOnce({ success: true })
      .mockResolvedValueOnce({
        provider: "openai",
        model: "gpt-5.2",
        reasoning_effort: "medium",
        selection_status: { status: "selectable", selectable: true, runnable: true },
      })
      .mockResolvedValueOnce({
        provider: "openai",
        model: "gpt-5.2",
        reasoning_effort: "high",
        selection_status: { status: "selectable", selectable: true, runnable: true },
      });

    await fetchLLMProviderCredential("openai");
    await saveLLMProviderCredential("openai", { api_key: "sk-value", enabled: true });
    await testLLMProviderCredential("openai", { api_key: "sk-test" });
    await deleteLLMProviderCredential("openai");
    await expect(fetchReportingLLMSelection()).resolves.toEqual({
      provider: "openai",
      model: "gpt-5.2",
      reasoningEffort: "medium",
      selectionStatus: { status: "selectable", selectable: true, runnable: true },
    });
    await expect(
      saveReportingLLMSelection({
        provider: "openai",
        model: "gpt-5.2",
        reasoning_effort: "high",
      }),
    ).resolves.toEqual({
      provider: "openai",
      model: "gpt-5.2",
      reasoningEffort: "high",
      selectionStatus: { status: "selectable", selectable: true, runnable: true },
    });

    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      1,
      "/api/llm/providers/openai/credential",
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      2,
      "/api/llm/providers/openai/credential",
      {
        method: "PUT",
        body: JSON.stringify({ api_key: "sk-value", enabled: true }),
      },
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      3,
      "/api/llm/providers/openai/credential/test",
      {
        method: "POST",
        body: JSON.stringify({ api_key: "sk-test" }),
      },
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      4,
      "/api/llm/providers/openai/credential",
      { method: "DELETE" },
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      5,
      "/api/llm/reporting-selection",
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      6,
      "/api/llm/reporting-selection",
      {
        method: "PUT",
        body: JSON.stringify({
          provider: "openai",
          model: "gpt-5.2",
          reasoning_effort: "high",
        }),
      },
    );
  });
});
