// @vitest-environment jsdom
/**
 * Verifies provider-neutral LLM credential settings flows.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ProviderCredentialCard from "../ProviderCredentialCard";
import ProviderSettingsSection from "../ProviderSettingsSection";
import type { LLMCatalogProvider, LLMModelCatalogResponse } from "../types";

const mocked = vi.hoisted(() => ({
  createLLMManagedConnection: vi.fn(),
  deleteLLMProviderCredential: vi.fn(),
  enableLLMManagedConnection: vi.fn(),
  fetchLLMModelCatalog: vi.fn(),
  fetchLLMSelection: vi.fn(),
  fetchReportingLLMSelection: vi.fn(),
  refreshLLMManagedConnectionInventory: vi.fn(),
  saveLLMDeploymentSelection: vi.fn(),
  saveLLMProviderCredential: vi.fn(),
  saveReportingLLMSelection: vi.fn(),
  testLLMManagedConnection: vi.fn(),
  testLLMProviderCredential: vi.fn(),
}));

vi.mock("../api", () => mocked);

const openAIProvider: LLMCatalogProvider = {
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
    masked_api_key: "sk-...1234",
  },
  defaultModel: "gpt-5.2",
  models: [
    {
      id: "gpt-5.2",
      label: "GPT-5.2",
      apiSurface: "responses",
      capabilities: ["chat", "reasoning_effort"],
      contextWindowTokens: 128000,
      maxOutputTokens: 32000,
      reasoningEfforts: ["minimal", "low", "medium", "high"],
      visibleReasoningEfforts: ["low", "medium", "high"],
      defaultReasoningEffort: "minimal",
      defaultVisibleReasoningEffort: "medium",
      toolChoiceModes: ["auto"],
      structuredOutputStrategies: ["native_schema"],
      pricingStatus: "priced",
    },
  ],
};

const anthropicProvider: LLMCatalogProvider = {
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
      id: "claude-sonnet-4-6",
      label: "Claude Sonnet 4.6",
      apiSurface: "messages",
      capabilities: ["chat"],
      contextWindowTokens: 1000000,
      maxOutputTokens: 64000,
      reasoningEfforts: [],
      visibleReasoningEfforts: [],
      defaultReasoningEffort: null,
      defaultVisibleReasoningEffort: null,
      toolChoiceModes: ["auto"],
      structuredOutputStrategies: ["native_schema"],
      pricingStatus: "priced",
    },
  ],
};

const catalog: LLMModelCatalogResponse = {
  providers: [openAIProvider, anthropicProvider],
};

function renderWithQueryClient(component: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      {component}
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocked.fetchLLMSelection.mockResolvedValue({
    provider: "openai",
    model: "gpt-5.2",
    deploymentRef: null,
    selectionStatus: { status: "selectable", selectable: true, runnable: true },
  });
  mocked.fetchReportingLLMSelection.mockResolvedValue({
    provider: null,
    model: null,
    reasoningEffort: null,
    selectionStatus: { runnable: false, reason: "No reporting model selected." },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProviderSettingsSection", () => {
  it("shows a catalog load error instead of the empty-provider state", async () => {
    mocked.fetchLLMModelCatalog.mockRejectedValue(new Error("Catalog request failed"));

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(await screen.findByText("Unable to load LLM providers")).toBeTruthy();
    expect(screen.getByText("Catalog request failed")).toBeTruthy();
    expect(screen.queryByText("No providers available")).toBeNull();
  });

  it("renders provider credential cards from the catalog without model selection controls", async () => {
    mocked.fetchLLMModelCatalog.mockResolvedValue(catalog);

    renderWithQueryClient(
      <ProviderSettingsSection
        queryEnabled
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    expect(await screen.findByRole("heading", { name: "AI providers" })).toBeTruthy();
    expect(await screen.findByText("OpenAI")).toBeTruthy();
    expect(screen.getAllByText("OpenAI").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Anthropic").length).toBeGreaterThan(0);
    expect(screen.queryByText("Reporting model")).toBeNull();
    expect(screen.queryByText("Workload deployment")).toBeNull();
    expect(screen.queryByRole("button", { name: "Advanced model preferences" })).toBeNull();
    expect(screen.queryByText(/capability evidence|lifecycle|runnability/i)).toBeNull();
    expect(screen.queryByRole("combobox", { name: /selected provider/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /save model selection/i })).toBeNull();
  });
});

describe("ProviderCredentialCard", () => {
  it("connects, verifies, and disconnects provider credentials without using legacy OpenAI settings", async () => {
    mocked.saveLLMProviderCredential.mockResolvedValue(openAIProvider.credential);
    mocked.testLLMProviderCredential.mockResolvedValue({
      provider: "openai",
      status: "ok",
      message: "ok",
      model_count: 3,
    });
    mocked.deleteLLMProviderCredential.mockResolvedValue({ success: true });

    renderWithQueryClient(
      <ProviderCredentialCard
        provider={openAIProvider}
        onSuccess={() => undefined}
        onError={() => undefined}
      />,
    );

    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "sk-test-value" },
    });
    fireEvent.click(screen.getByRole("button", { name: /update openai/i }));

    await waitFor(() => {
      expect(mocked.saveLLMProviderCredential).toHaveBeenCalledWith("openai", {
        api_key: "sk-test-value",
        enabled: true,
      });
      expect(mocked.testLLMProviderCredential).toHaveBeenCalledWith("openai", {
        api_key: "sk-test-value",
      });
    });

    fireEvent.click(screen.getByRole("button", { name: /disconnect openai/i }));
    await waitFor(() => {
      expect(mocked.deleteLLMProviderCredential).toHaveBeenCalledWith("openai");
    });
  });
});
