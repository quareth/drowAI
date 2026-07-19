// @vitest-environment jsdom
/**
 * Verifies deployment-aware picker behavior from backend catalog metadata.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import DeploymentPicker from "../DeploymentPicker";
import type { LLMModelCatalogResponse } from "../types";

const deploymentRef = {
  deployment_id: "11111111-1111-4111-8111-111111111111",
  expected_revision: 2,
};

const legacyDefaultDeploymentRef = {
  deployment_id: "44444444-4444-4444-8444-444444444444",
  expected_revision: 1,
};

const catalog: LLMModelCatalogResponse = {
  providers: [
    {
      id: "huggingface",
      label: "Hugging Face",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "huggingface",
        enabled: true,
        has_api_key: true,
      },
      defaultModel: "openai/gpt-oss-20b:fireworks-ai",
      models: [
        {
          id: "openai/gpt-oss-20b:fireworks-ai",
          canonicalModelId: "openai/gpt-oss-20b",
          exactWireModelId: "openai/gpt-oss-20b:fireworks-ai",
          label: "GPT-OSS 20B via HF",
          apiSurface: "chat_completions",
          capabilities: ["chat", "usage_reporting"],
          contextWindowTokens: 128000,
          maxOutputTokens: 8192,
          reasoningEfforts: [],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: null,
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: ["auto"],
          structuredOutputStrategies: [],
          pricingStatus: "unavailable",
          deploymentRef,
          runnable: true,
          connection: {
            presetId: "hf-preset",
            displayName: "Hugging Face Router",
            enabled: true,
            authMode: "bearer_api_key",
            userConfigFields: ["api_key"],
            configFields: [],
            lifecycleState: "enabled",
            connectionRef: {
              connection_id: "22222222-2222-4222-8222-222222222222",
              expected_revision: 3,
            },
            deploymentRef,
            verification: {
              status: "passed",
              code: "verified",
              message: "Verified",
              retryable: false,
            },
            runnability: {
              status: "runnable",
              selectable: true,
              runnable: true,
              reason: null,
            },
          },
          proving: null,
        },
      ],
    },
    {
      id: "team",
      label: "Team Hosted",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "team",
        enabled: true,
        has_api_key: true,
      },
      defaultModel: "team/tool-model",
      models: [
        {
          id: "team/tool-model",
          label: "Team Tool Model",
          apiSurface: "chat_completions",
          capabilities: ["chat"],
          contextWindowTokens: 32000,
          maxOutputTokens: 4096,
          reasoningEfforts: [],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: null,
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: ["auto"],
          structuredOutputStrategies: [],
          pricingStatus: "unavailable",
          deploymentRef: {
            deployment_id: "33333333-3333-4333-8333-333333333333",
            expected_revision: 1,
          },
          runnable: false,
          connection: {
            presetId: "team-preset",
            displayName: "Team vLLM",
            enabled: true,
            authMode: "bearer_api_key",
            userConfigFields: ["api_key"],
            configFields: [],
            lifecycleState: "disabled",
            connectionRef: null,
            deploymentRef: null,
            verification: null,
            runnability: {
              status: "capability_unknown",
              selectable: true,
              runnable: false,
              reason: "Usage evidence is required.",
            },
          },
          proving: null,
        },
      ],
    },
  ],
};

const legacyDefaultCatalog: LLMModelCatalogResponse = {
  providers: [
    {
      id: "openai",
      label: "OpenAI",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "openai",
        enabled: true,
        has_api_key: true,
      },
      defaultModel: "gpt-5.2",
      models: [
        {
          id: "gpt-5.2",
          canonicalModelId: "gpt-5.2",
          exactWireModelId: null,
          label: "GPT-5.2",
          apiSurface: "responses",
          capabilities: ["chat", "tool_calling"],
          contextWindowTokens: 400000,
          maxOutputTokens: 128000,
          reasoningEfforts: ["none", "low", "medium", "high"],
          visibleReasoningEfforts: [],
          defaultReasoningEffort: "medium",
          defaultVisibleReasoningEffort: null,
          toolChoiceModes: ["auto"],
          structuredOutputStrategies: ["json_schema"],
          pricingStatus: "available",
          deploymentRef: legacyDefaultDeploymentRef,
          runnable: true,
          proving: null,
        },
      ],
    },
  ],
};

afterEach(() => {
  cleanup();
});

describe("DeploymentPicker", () => {
  it("groups searchable deployment choices and preserves unavailable pricing", () => {
    const onSelectDeployment = vi.fn();

    render(
      <DeploymentPicker
        catalog={catalog}
        selectedDeploymentRef={null}
        onSelectDeployment={onSelectDeployment}
      />,
    );

    expect(screen.getByText("Hugging Face")).toBeTruthy();
    expect(screen.getByText("Team Hosted")).toBeTruthy();
    expect(screen.getByText("GPT-OSS 20B via HF")).toBeTruthy();
    expect(screen.getAllByText(/Pricing: unavailable/i).length).toBeGreaterThan(0);
    expect(screen.queryByText("$0")).toBeNull();
    expect(screen.getByText("Current/default")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("Search deployments"), {
      target: { value: "tool" },
    });

    expect(screen.queryByText("GPT-OSS 20B via HF")).toBeNull();
    expect(screen.getByText("Team Tool Model")).toBeTruthy();
    expect(screen.getByText("Usage evidence is required.")).toBeTruthy();
  });

  it("selects only runnable backend-provided deployment refs", () => {
    const onSelectDeployment = vi.fn();

    render(
      <DeploymentPicker
        catalog={catalog}
        selectedDeploymentRef={null}
        onSelectDeployment={onSelectDeployment}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /select gpt-oss 20b via hf/i }));
    expect(onSelectDeployment).toHaveBeenCalledWith(deploymentRef);

    const disabledButton = screen.getByRole("button", {
      name: /select team tool model/i,
    }) as HTMLButtonElement;
    expect(disabledButton.disabled).toBe(true);
  });

  it("selects legacy default catalog rows with only top-level deployment refs", () => {
    const onSelectDeployment = vi.fn();

    render(
      <DeploymentPicker
        catalog={legacyDefaultCatalog}
        selectedDeploymentRef={null}
        onSelectDeployment={onSelectDeployment}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /select gpt-5\.2/i }));
    expect(onSelectDeployment).toHaveBeenCalledWith(legacyDefaultDeploymentRef);
  });
});
