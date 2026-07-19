// @vitest-environment jsdom
/**
 * Verifies deployment-aware picker behavior from backend catalog metadata.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import DeploymentPicker from "../DeploymentPicker";
import type { LLMCatalogProvider, LLMModelCatalogResponse } from "../types";

const deploymentRef = {
  deployment_id: "11111111-1111-4111-8111-111111111111",
  expected_revision: 2,
};

const nimDeploymentRef = {
  deployment_id: "55555555-5555-4555-8555-555555555555",
  expected_revision: 4,
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
      id: "nvidia_nim",
      label: "NVIDIA NIM",
      capabilities: [],
      available: true,
      selectable: true,
      credential: {
        user_id: 1,
        provider: "nvidia_nim",
        enabled: true,
        has_api_key: true,
      },
      defaultModel: "openai/gpt-oss-20b",
      models: [
        {
          id: "openai/gpt-oss-20b",
          canonicalModelId: "openai/gpt-oss-20b",
          exactWireModelId: "openai/gpt-oss-20b",
          label: "GPT-OSS 20B via NIM",
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
          deploymentRef: nimDeploymentRef,
          runnable: true,
          connection: {
            presetId: "nim-preset",
            displayName: "NVIDIA NIM Endpoint",
            enabled: true,
            authMode: "bearer_api_key",
            userConfigFields: ["api_key"],
            configFields: [],
            lifecycleState: "enabled",
            connectionRef: {
              connection_id: "66666666-6666-4666-8666-666666666666",
              expected_revision: 2,
            },
            deploymentRef: nimDeploymentRef,
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

function gptOssProvider(
  id: string,
  label: string,
  deploymentId: string,
  wireModelId: string,
): LLMCatalogProvider {
  const ref = {
    deployment_id: deploymentId,
    expected_revision: 1,
  };
  return {
    id,
    label,
    capabilities: [],
    available: true,
    selectable: true,
    credential: {
      user_id: 1,
      provider: id,
      enabled: true,
      has_api_key: true,
    },
    defaultModel: wireModelId,
    models: [
      {
        id: wireModelId,
        canonicalModelId: "openai/gpt-oss-20b",
        exactWireModelId: wireModelId,
        label: `GPT-OSS 20B via ${label}`,
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
        deploymentRef: ref,
        runnable: true,
        connection: {
          presetId: id,
          displayName: label,
          enabled: true,
          authMode: "bearer_api_key",
          userConfigFields: ["api_key"],
          configFields: [],
          lifecycleState: "enabled",
          connectionRef: null,
          deploymentRef: ref,
          verification: null,
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
  };
}

afterEach(() => {
  cleanup();
});

describe("DeploymentPicker", () => {
  it("labels proving choices with backend deployment display identity", () => {
    const provingRef = {
      deployment_id: "77777777-7777-4777-8777-777777777777",
      expected_revision: 5,
    };
    const onSelectDeployment = vi.fn();

    render(
      <DeploymentPicker
        catalog={{
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
              defaultModel: "gpt-oss-20b",
              models: [
                {
                  id: "gpt-oss-20b",
                  canonicalModelId: "openai/gpt-oss-20b",
                  exactWireModelId: "openai/gpt-oss-20b",
                  label: "GPT-OSS 20B",
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
                  deploymentRef: provingRef,
                  runnable: true,
                  proving: {
                    presetId: "gpt_oss_20b_openai_compatible_proving",
                    displayName: "GPT-OSS 20B OpenAI-compatible proving",
                    enabled: true,
                    authMode: "bearer_api_key",
                    userConfigFields: ["api_key"],
                    lifecycleState: "enabled",
                    connectionRef: {
                      connection_id: "88888888-8888-4888-8888-888888888888",
                      expected_revision: 6,
                    },
                    deploymentRef: provingRef,
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
                },
              ],
            },
          ],
        }}
        selectedDeploymentRef={null}
        onSelectDeployment={onSelectDeployment}
      />,
    );

    expect(screen.getAllByRole("heading", { name: "GPT-OSS 20B" })).toHaveLength(1);
    expect(screen.getByText("GPT-OSS 20B OpenAI-compatible proving")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /openai-compatible proving/i }));
    expect(onSelectDeployment).toHaveBeenCalledWith(provingRef);
  });

  it("renders one canonical GPT-OSS model group with explicit deployment choices", () => {
    const onSelectDeployment = vi.fn();

    render(
      <DeploymentPicker
        catalog={catalog}
        selectedDeploymentRef={null}
        onSelectDeployment={onSelectDeployment}
      />,
    );

    expect(screen.getAllByRole("heading", { name: "GPT-OSS 20B" })).toHaveLength(1);
    expect(screen.getByText("Hugging Face")).toBeTruthy();
    expect(screen.getByText("NVIDIA NIM")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /nvidia nim/i }));
    expect(onSelectDeployment).toHaveBeenCalledWith(nimDeploymentRef);
  });

  it("keeps all named GPT-OSS serving operators beneath one model heading", () => {
    render(
      <DeploymentPicker
        catalog={{
          providers: [
            gptOssProvider(
              "gpt_oss_20b_openai_compatible_proving",
              "GPT-OSS proving",
              "10000000-0000-4000-8000-000000000001",
              "openai/gpt-oss-20b",
            ),
            gptOssProvider(
              "huggingface_openai_compatible_chat",
              "Hugging Face",
              "10000000-0000-4000-8000-000000000002",
              "openai/gpt-oss-20b:fireworks-ai",
            ),
            gptOssProvider(
              "nvidia_nim_openai_compatible_chat",
              "NVIDIA NIM",
              "10000000-0000-4000-8000-000000000003",
              "openai/gpt-oss-20b",
            ),
            gptOssProvider(
              "ollama_openai_compatible_chat",
              "Ollama",
              "10000000-0000-4000-8000-000000000004",
              "gpt-oss:20b",
            ),
            gptOssProvider(
              "vllm_openai_compatible_chat",
              "vLLM",
              "10000000-0000-4000-8000-000000000005",
              "openai/gpt-oss-20b",
            ),
            gptOssProvider(
              "custom_openai_compatible_chat",
              "Custom",
              "10000000-0000-4000-8000-000000000006",
              "team/gpt-oss-20b",
            ),
          ],
        }}
        selectedDeploymentRef={null}
        onSelectDeployment={() => undefined}
      />,
    );

    expect(screen.getAllByRole("heading", { name: "GPT-OSS 20B" })).toHaveLength(1);
    for (const operator of [
      "GPT-OSS proving",
      "Hugging Face",
      "NVIDIA NIM",
      "Ollama",
      "vLLM",
      "Custom",
    ]) {
      expect(screen.getByText(operator)).toBeTruthy();
    }
  });

  it("groups searchable deployment choices and preserves unavailable pricing", () => {
    const onSelectDeployment = vi.fn();

    render(
      <DeploymentPicker
        catalog={catalog}
        selectedDeploymentRef={deploymentRef}
        onSelectDeployment={onSelectDeployment}
      />,
    );

    expect(screen.getByText("Hugging Face")).toBeTruthy();
    expect(screen.getByText("Team Hosted")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "GPT-OSS 20B" })).toBeTruthy();
    expect(screen.getByText("Wire: openai/gpt-oss-20b:fireworks-ai")).toBeTruthy();
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

    fireEvent.click(screen.getByRole("button", { name: /hugging face/i }));
    expect(onSelectDeployment).toHaveBeenCalledWith(deploymentRef);

    const disabledButton = screen.getByRole("button", {
      name: /team hosted/i,
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
