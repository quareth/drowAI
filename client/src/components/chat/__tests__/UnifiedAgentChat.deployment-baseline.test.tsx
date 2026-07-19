// @vitest-environment jsdom
/**
 * Verifies UnifiedAgentChat uses the canonical global model mutation path.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { UnifiedAgentChat } from "../UnifiedAgentChat";
import type { LLMDeploymentRef } from "@/features/llm-provider/types";

const mocked = vi.hoisted(() => {
  const initialDeploymentRef: LLMDeploymentRef = {
    deployment_id: "deployment-gpt-oss-hf",
    expected_revision: 3,
  };
  const alternateDeploymentRef: LLMDeploymentRef = {
    deployment_id: "deployment-gpt-oss-nim",
    expected_revision: 5,
  };

  return {
    initialDeploymentRef,
    alternateDeploymentRef,
    saveLLMDeploymentSelection: vi.fn(),
    saveLLMSelection: vi.fn(),
    fetchLLMModelCatalog: vi.fn(),
    fetchLLMSelection: vi.fn(),
    apiRequest: vi.fn(),
    toast: vi.fn(),
  };
});

vi.mock("@/features/llm-provider/api", () => ({
  fetchLLMModelCatalog: mocked.fetchLLMModelCatalog,
  fetchLLMSelection: mocked.fetchLLMSelection,
  saveLLMDeploymentSelection: mocked.saveLLMDeploymentSelection,
  saveLLMSelection: mocked.saveLLMSelection,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocked.toast }),
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequest,
}));

vi.mock("@/config/feature-flags", () => ({
  featureFlags: {
    enableBasicChat: true,
    enableSendQueueUI: false,
  },
}));

vi.mock("@/hooks/useUnifiedChat", () => ({
  useUnifiedChat: (options: {
    orchestrator: {
      orchestrateMessageFlow: (message: string, mode: "interactive") => Promise<void>;
    };
  }) => ({
    messages: [],
    isLoading: false,
    isConnected: true,
    connectionError: null,
    sendMessage: (message: string) => options.orchestrator.orchestrateMessageFlow(message, "interactive"),
    loadMore: vi.fn(),
    hasMore: false,
  }),
}));

vi.mock("@/hooks/useChatBootstrap", () => ({
  useChatBootstrap: () => ({
    isReady: true,
    isPending: false,
    statusMessage: null,
    error: null,
  }),
}));

vi.mock("@/hooks/useTaskRunState", () => ({
  useTaskRunState: () => ({
    42: {
      state: "idle",
      turnId: null,
      cancelRequested: false,
      isStreaming: false,
      queuedCount: 0,
      isActiveGeneration: false,
      canStop: false,
    },
  }),
}));

vi.mock("@/hooks/useInterruptState", () => ({
  useInterruptState: () => ({
    interrupt: null,
    setInterrupt: vi.fn(),
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useGraphResume", () => ({
  useGraphResume: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useGraphRetry", () => ({
  useGraphRetry: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useChatStop", () => ({
  useChatStop: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useSendQueue", () => ({
  useSendQueue: () => ({
    items: [],
    count: 0,
    onUserSend: vi.fn(),
    remove: vi.fn(),
    modify: vi.fn(),
    clear: vi.fn(),
  }),
}));

vi.mock("@/state/context-window-store", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/state/context-window-store")>();
  return {
    ...actual,
    useContextCompactionGate: () => null,
  };
});

vi.mock("../TaskModelSelectors", () => ({
  default: (props: {
    selectedSelection: { deploymentRef?: LLMDeploymentRef | null } | null;
    onModelChange: (selection: {
      provider: string;
      model: string;
      deploymentRef: LLMDeploymentRef;
    }) => void;
  }) => (
    <div>
      <span data-testid="selected-deployment">
        {props.selectedSelection?.deploymentRef?.deployment_id ?? "none"}
      </span>
      <button
        type="button"
        onClick={() =>
          props.onModelChange({
            provider: "gpt-oss",
            model: "gpt-oss-20b",
            deploymentRef: mocked.alternateDeploymentRef,
          })
        }
      >
        Choose alternate deployment
      </button>
    </div>
  ),
}));

vi.mock("../MessageList", () => ({
  default: () => <div data-testid="message-list" />,
}));

vi.mock("../ChatInput", () => ({
  default: (props: { onSend: (message: string) => void | Promise<void> }) => (
    <button type="button" onClick={() => props.onSend("hello deployment")}>
      Send test message
    </button>
  ),
}));

vi.mock("@/components/panels/PlanCard", () => ({
  PlanCard: () => null,
}));

const source = readFileSync(
  resolve(process.cwd(), "client/src/components/chat/UnifiedAgentChat.tsx"),
  "utf8",
);

afterEach(() => {
  cleanup();
  mocked.saveLLMDeploymentSelection.mockReset();
  mocked.saveLLMSelection.mockReset();
  mocked.fetchLLMModelCatalog.mockReset();
  mocked.fetchLLMSelection.mockReset();
  mocked.apiRequest.mockReset();
  mocked.toast.mockReset();
});

function renderChatWithDeploymentSelection() {
  const catalog = { providers: [] };
  const selection = {
    provider: "gpt-oss",
    model: "gpt-oss-20b",
    deploymentRef: mocked.initialDeploymentRef,
    selectionStatus: {
      status: "selectable",
      selectable: true,
      runnable: true,
      reason: null,
    },
  };
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  queryClient.setQueryData(["/api/tasks/"], [
    {
      id: 42,
      name: "Phase 8 task",
      status: "running",
      created_at: "2026-07-19T00:00:00Z",
    },
  ]);
  queryClient.setQueryData(["/api/llm/models"], catalog);
  queryClient.setQueryData(["/api/llm/selection"], selection);
  mocked.fetchLLMModelCatalog.mockResolvedValue(catalog);
  mocked.fetchLLMSelection.mockResolvedValue(selection);
  mocked.saveLLMDeploymentSelection.mockResolvedValue({
    provider: "gpt-oss",
    model: "gpt-oss-20b",
    deploymentRef: mocked.alternateDeploymentRef,
  });
  mocked.apiRequest.mockResolvedValue({
    ok: true,
    json: async () => ({ success: true, conversation_id: "conv-42", turn_id: "turn-1" }),
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <UnifiedAgentChat taskId={42} />
    </QueryClientProvider>,
  );
}

describe("UnifiedAgentChat deployment baseline", () => {
  it("uses llm-provider api helpers for catalog and global selection", () => {
    expect(source).toContain("fetchLLMModelCatalog");
    expect(source).toContain("fetchLLMSelection");
    expect(source).toContain("saveLLMSelection");
    expect(source).toContain('from "@/features/llm-provider/api"');
  });

  it("uses one user-global selection mutation without a task-switch request", () => {
    expect(source).toContain("updateSelection.mutate(selection)");
    expect(source).not.toContain("switchTaskModel");
    expect(source).not.toContain("/api/llm/tasks/${taskIdentifier}/switch");
  });

  it("saves a same model/provider selection when the deployment ref changes", async () => {
    renderChatWithDeploymentSelection();

    await waitFor(() => {
      expect(screen.getByTestId("selected-deployment").textContent).toBe(
        mocked.initialDeploymentRef.deployment_id,
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Choose alternate deployment" }));

    await waitFor(() => {
      expect(mocked.saveLLMDeploymentSelection).toHaveBeenCalledWith({
        deployment_ref: mocked.alternateDeploymentRef,
      });
    });
    expect(mocked.saveLLMSelection).not.toHaveBeenCalled();
  });

  it("sends the selected deployment ref with the next chat turn", async () => {
    renderChatWithDeploymentSelection();

    await waitFor(() => {
      expect(screen.getByTestId("selected-deployment").textContent).toBe(
        mocked.initialDeploymentRef.deployment_id,
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Choose alternate deployment" }));

    await waitFor(() => {
      expect(screen.getByTestId("selected-deployment").textContent).toBe(
        mocked.alternateDeploymentRef.deployment_id,
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Send test message" }));

    await waitFor(() => {
      expect(mocked.apiRequest).toHaveBeenCalled();
    });
    expect(mocked.apiRequest).toHaveBeenCalledWith(
      "POST",
      "/api/tasks/42/chat",
      expect.objectContaining({
        provider: "gpt-oss",
        model: "gpt-oss-20b",
        deployment_ref: mocked.alternateDeploymentRef,
      }),
    );
  });
});
