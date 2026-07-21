/**
 * Unit tests for provider-neutral LLM catalog helpers.
 */

import { describe, expect, it } from "vitest";

import {
  findSelectedCatalogEntry,
  getBlockingSelectionStatus,
  getFirstCatalogDefaultSelection,
  isSelectionSelectable,
  getSelectedModelDisplayLabel,
} from "../catalog";
import {
  getVisibleReasoningEffortOptions,
  getSupportedReasoningEffortForPayload,
  shouldOmitReasoningEffort,
  supportsNativeStructuredOutput,
  supportsTools,
} from "../capability-controls";
import type { LLMCatalogModel, LLMModelCatalogResponse } from "../types";

const catalog: LLMModelCatalogResponse = {
  providers: [
    {
      id: "openai",
      label: "OpenAI",
      capabilities: ["remote_conversation_lifecycle"],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "openai",
        enabled: true,
        has_api_key: true,
        masked_api_key: "sk-...1234",
      },
      defaultModel: "gpt-5.2",
      models: [
        {
          id: "gpt-5.2",
          label: "GPT-5.2",
          apiSurface: "responses",
          capabilities: ["chat", "reasoning_effort", "tools", "structured_output_native"],
          contextWindowTokens: 128000,
          maxOutputTokens: 32000,
          reasoningEfforts: ["none", "minimal", "low", "medium", "high"],
          visibleReasoningEfforts: ["low", "medium", "high"],
          defaultReasoningEffort: "minimal",
          defaultVisibleReasoningEffort: "medium",
          toolChoiceModes: ["auto", "none", "required", "specific"],
          structuredOutputStrategies: ["native_schema"],
          pricingStatus: "priced",
        },
      ],
    },
    {
      id: "anthropic",
      label: "Anthropic",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "anthropic",
        enabled: false,
        has_api_key: false,
      },
      defaultModel: "claude-sonnet-4-6",
      models: [
        {
          id: "claude-sonnet-5",
          label: "Claude Sonnet 5",
          apiSurface: "messages",
          capabilities: ["chat", "reasoning_effort", "tools"],
          contextWindowTokens: 1000000,
          maxOutputTokens: 128000,
          reasoningEfforts: ["low", "medium", "high", "xhigh", "max"],
          visibleReasoningEfforts: ["low", "medium", "high", "xhigh", "max"],
          defaultReasoningEffort: "high",
          defaultVisibleReasoningEffort: "high",
          toolChoiceModes: ["auto", "none", "required", "specific"],
          structuredOutputStrategies: ["prompt_parse"],
          pricingStatus: "priced",
        },
        {
          id: "claude-sonnet-4-6",
          label: "Claude Sonnet 4.6",
          apiSurface: "messages",
          capabilities: ["chat", "tools", "structured_output_native"],
          contextWindowTokens: 1000000,
          maxOutputTokens: 64000,
          reasoningEfforts: [],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: null,
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: ["auto", "none", "required", "specific"],
          structuredOutputStrategies: ["native_schema"],
          pricingStatus: "priced",
        },
      ],
    },
  ],
};

describe("LLM provider catalog helpers", () => {
  it("finds provider/model pairs without model-id inference", () => {
    const entry = findSelectedCatalogEntry(catalog, {
      provider: "anthropic",
      model: "claude-sonnet-4-6",
    });

    expect(entry?.provider.label).toBe("Anthropic");
    expect(entry?.model.label).toBe("Claude Sonnet 4.6");
  });

  it("treats deployment identity as authoritative over compatibility fields", () => {
    const deploymentRef = {
      deployment_id: "11111111-1111-4111-8111-111111111111",
      expected_revision: 2,
    };
    const deploymentCatalog: LLMModelCatalogResponse = {
      providers: [{
        ...catalog.providers[0],
        models: [{ ...catalog.providers[0].models[0], deploymentRef }],
      }],
    };

    const entry = findSelectedCatalogEntry(deploymentCatalog, {
      provider: "nvidia_nim_openai_compatible_chat",
      model: "openai/gpt-oss-20b",
      deploymentRef,
    });

    expect(entry?.provider.id).toBe("openai");
    expect(entry?.model.id).toBe("gpt-5.2");
    expect(findSelectedCatalogEntry(deploymentCatalog, {
      provider: "openai",
      model: "gpt-5.2",
      deploymentRef: {
        deployment_id: "22222222-2222-4222-8222-222222222222",
        expected_revision: 1,
      },
    })).toBeNull();
  });

  it("derives labels and backend-owned defaults from catalog metadata", () => {
    expect(getSelectedModelDisplayLabel(catalog, { provider: "openai", model: "gpt-5.2" })).toBe("GPT-5.2");
    expect(getSelectedModelDisplayLabel(
      catalog,
      { provider: "openai", model: "gpt-5.2" },
      { includeProvider: true },
    )).toBe("OpenAI / GPT-5.2");
    expect(getFirstCatalogDefaultSelection(catalog)).toEqual({ provider: "openai", model: "gpt-5.2" });
  });

  it("does not use unavailable providers as automatic defaults", () => {
    const unavailableCatalog: LLMModelCatalogResponse = {
      providers: [
        { ...catalog.providers[0], available: false, selectable: false },
        { ...catalog.providers[1], available: false, selectable: false },
      ],
    };

    expect(getFirstCatalogDefaultSelection(unavailableCatalog)).toBeNull();
    expect(isSelectionSelectable(unavailableCatalog, { provider: "anthropic", model: "claude-sonnet-4-6" })).toBe(false);
  });

  it("gates reasoning controls from model metadata", () => {
    expect(getVisibleReasoningEffortOptions(catalog.providers[0].models[0])).toEqual(["low", "medium", "high"]);
    expect(shouldOmitReasoningEffort(catalog, { provider: "openai", model: "gpt-5.2" }, "medium")).toBe(false);
    expect(shouldOmitReasoningEffort(catalog, { provider: "anthropic", model: "claude-sonnet-4-6" }, "medium")).toBe(true);
    expect(getSupportedReasoningEffortForPayload(
      catalog,
      { provider: "openai", model: "gpt-5.2" },
      "medium",
    )).toBe("medium");
    const gpt56: LLMCatalogModel = {
      ...catalog.providers[0].models[0],
      id: "gpt-5.6-sol",
      reasoningEfforts: ["none", "low", "medium", "high", "xhigh", "max"],
      visibleReasoningEfforts: ["low", "medium", "high", "xhigh", "max"],
      defaultReasoningEffort: "medium",
      defaultVisibleReasoningEffort: "medium",
    };
    expect(getVisibleReasoningEffortOptions(gpt56)).toEqual(["low", "medium", "high", "xhigh", "max"]);
    expect(getVisibleReasoningEffortOptions(catalog.providers[1].models[0])).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
    expect(getSupportedReasoningEffortForPayload(
      catalog,
      { provider: "anthropic", model: "claude-sonnet-5" },
      "xhigh",
    )).toBe("xhigh");
    expect(getSupportedReasoningEffortForPayload(
      catalog,
      { provider: "anthropic", model: "claude-sonnet-4-6" },
      "medium",
    )).toBeUndefined();
    expect(getSupportedReasoningEffortForPayload(
      undefined,
      { provider: "openai", model: "gpt-5.2" },
      "medium",
    )).toBe("medium");
    expect(getSupportedReasoningEffortForPayload(
      undefined,
      { provider: "anthropic", model: "claude-sonnet-4-6" },
      "medium",
    )).toBeUndefined();
  });

  it("gates tool and structured-output flows from catalog metadata", () => {
    const anthropic = { provider: "anthropic", model: "claude-sonnet-4-6" };
    expect(supportsTools(catalog, anthropic)).toBe(true);
    expect(supportsNativeStructuredOutput(catalog, anthropic)).toBe(true);
  });

  it("classifies non-runnable saved selections only for the active selection", () => {
    const savedSelection = {
      provider: "anthropic",
      model: "claude-sonnet-4-6",
      selectionStatus: {
        status: "credential_missing",
        selectable: false,
        runnable: false,
        reason: "anthropic credential is required to run anthropic model",
      },
    };

    expect(getBlockingSelectionStatus(
      savedSelection,
      { provider: "anthropic", model: "claude-sonnet-4-6" },
    )?.status).toBe("credential_missing");
    expect(getBlockingSelectionStatus(
      savedSelection,
      { provider: "openai", model: "gpt-5.2" },
    )).toBeNull();
  });
});
