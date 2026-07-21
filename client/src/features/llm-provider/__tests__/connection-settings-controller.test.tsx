/**
 * Directly verifies the managed connection controller public contract.
 *
 * These tests cover lifecycle orchestration, callback ordering, and cache
 * invalidation without reaching into controller internals or rendered UI.
 *
 * @vitest-environment jsdom
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useConnectionSettingsController } from "../useConnectionSettingsController";
import type {
  LLMCatalogModel,
  LLMConnectionMetadata,
  LLMConnectionRef,
  LLMDeploymentRef,
  LLMProvingConnectionStatus,
  LLMProvingVerification,
} from "../types";

const mocked = vi.hoisted(() => ({
  createLLMManagedConnection: vi.fn(),
  enableLLMManagedConnection: vi.fn(),
  refreshLLMManagedConnectionInventory: vi.fn(),
  testLLMManagedConnection: vi.fn(),
}));

vi.mock("../api", () => mocked);

const connectionRef: LLMConnectionRef = {
  connection_id: "11111111-1111-4111-8111-111111111111",
  expected_revision: 2,
};

const refreshedConnectionRef: LLMConnectionRef = {
  connection_id: "22222222-2222-4222-8222-222222222222",
  expected_revision: 3,
};

const deploymentRef: LLMDeploymentRef = {
  deployment_id: "33333333-3333-4333-8333-333333333333",
  expected_revision: 4,
};

const refreshedDeploymentRef: LLMDeploymentRef = {
  deployment_id: "44444444-4444-4444-8444-444444444444",
  expected_revision: 5,
};

const baseModel: LLMCatalogModel = {
  id: "gpt-oss:20b",
  canonicalModelId: "openai/gpt-oss-20b",
  exactWireModelId: "openai/gpt-oss-20b",
  label: "GPT-OSS 20B via Ollama",
  apiSurface: "chat_completions",
  capabilities: ["chat"],
  contextWindowTokens: 128000,
  maxOutputTokens: 10000,
  reasoningEfforts: [],
  visibleReasoningEfforts: [],
  defaultReasoningEffort: null,
  defaultVisibleReasoningEffort: null,
  toolChoiceModes: ["auto"],
  structuredOutputStrategies: [],
  pricingStatus: "unavailable",
  deploymentRef: null,
  runnable: false,
};

const baseConnection: LLMConnectionMetadata = {
  presetId: "ollama_openai_compatible_chat",
  displayName: "Ollama-compatible HTTPS endpoint",
  enabled: true,
  authMode: "bearer_api_key",
  userConfigFields: ["display_label", "base_url", "api_key", "wire_model_id"],
  configFields: [
    {
      name: "display_label",
      label: "Display name",
      fieldType: "text",
      required: false,
      secret: false,
    },
    {
      name: "base_url",
      label: "Base URL",
      fieldType: "url",
      required: true,
      secret: false,
    },
    {
      name: "api_key",
      label: "API key",
      fieldType: "password",
      required: true,
      secret: true,
    },
    {
      name: "wire_model_id",
      label: "Model ID",
      fieldType: "text",
      required: true,
      secret: false,
    },
  ],
  lifecycleState: "not_created",
  connectionRef: null,
  deploymentRef: null,
  verification: null,
  runnability: {
    status: "not_created",
    selectable: true,
    runnable: false,
    reason: "Connection configuration is required.",
  },
};

const defaultFieldValues = {
  api_key: " sk-controller-placeholder ",
  base_url: " https://llm.example.test/team ",
  wire_model_id: " team/model ",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useConnectionSettingsController", () => {
  it("creates the exact request and enables without refresh when verification passes with effective refs", async () => {
    const onSuccess = vi.fn();
    const onError = vi.fn();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("passed"));
    mocked.enableLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: true,
    }));

    const { invalidateQueries, result } = renderController({ onSuccess, onError });

    expect(Object.keys(result.current).sort()).toEqual([
      "connect",
      "connectionRef",
      "isPending",
      "runnable",
    ]);
    expect(result.current.connectionRef).toBeNull();
    expect(result.current.runnable).toBe(false);

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(mocked.createLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          api_key: "sk-controller-placeholder",
          display_label: null,
          base_url: "https://llm.example.test/team",
          wire_model_id: "team/model",
          model_label: "GPT-OSS 20B via Ollama",
          canonical_model_id: "openai/gpt-oss-20b",
        },
      );
      expect(mocked.enableLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          connection_ref: connectionRef,
          deployment_ref: deploymentRef,
        },
      );
      expect(onSuccess).toHaveBeenCalledWith(
        "Ollama-compatible HTTPS endpoint connected",
        "GPT-OSS 20B is ready.",
      );
    });
    expect(mocked.testLLMManagedConnection).toHaveBeenCalledWith(
      "ollama_openai_compatible_chat",
      {
        api_key: "sk-controller-placeholder",
        connection_ref: connectionRef,
      },
    );
    expect(mocked.refreshLLMManagedConnectionInventory).not.toHaveBeenCalled();
    expect(result.current.connectionRef).toEqual(connectionRef);
    expect(result.current.runnable).toBe(true);
    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
    expectCallOrder(
      mocked.createLLMManagedConnection,
      mocked.testLLMManagedConnection,
      mocked.enableLLMManagedConnection,
    );
  });

  it("sends null canonical metadata when the canonical id has no distinct value", async () => {
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef: null,
      deploymentRef: null,
      runnable: false,
    }));

    const { result } = renderController({
      fieldValues: {
        api_key: " sk-controller-placeholder ",
        base_url: " https://llm.example.test/team ",
      },
      model: {
        ...baseModel,
        id: "team/default-model",
        canonicalModelId: "team/default-model",
        exactWireModelId: "team/wire-model",
        label: "Team Default Model",
      },
    });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(mocked.createLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          api_key: "sk-controller-placeholder",
          display_label: null,
          base_url: "https://llm.example.test/team",
          wire_model_id: "team/wire-model",
          model_label: "Team Default Model",
          canonical_model_id: null,
        },
      );
    });
    expect(mocked.testLLMManagedConnection).not.toHaveBeenCalled();
  });

  it("falls back to model id when wire model input and exact wire model are absent", async () => {
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef: null,
      deploymentRef: null,
      runnable: false,
    }));

    const { result } = renderController({
      fieldValues: {
        api_key: " sk-controller-placeholder ",
        base_url: " https://llm.example.test/team ",
        wire_model_id: "   ",
      },
      model: {
        ...baseModel,
        id: "team/model-id-fallback",
        canonicalModelId: "team/model-id-fallback",
        exactWireModelId: undefined,
        label: "Team Model ID Fallback",
      },
    });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(mocked.createLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          api_key: "sk-controller-placeholder",
          display_label: null,
          base_url: "https://llm.example.test/team",
          wire_model_id: "team/model-id-fallback",
          model_label: "Team Model ID Fallback",
          canonical_model_id: null,
        },
      );
    });
    expect(mocked.testLLMManagedConnection).not.toHaveBeenCalled();
  });

  it("fulfills early when creation yields no effective connection reference", async () => {
    const onSuccess = vi.fn();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef: null,
      deploymentRef: null,
      runnable: false,
    }));

    const { invalidateQueries, result } = renderController({ onSuccess });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith(
        "Ollama-compatible HTTPS endpoint connected",
        "The connection was saved, but GPT-OSS 20B is not ready yet.",
      );
    });
    expect(result.current.connectionRef).toBeNull();
    expect(result.current.runnable).toBe(false);
    expect(mocked.testLLMManagedConnection).not.toHaveBeenCalled();
    expect(mocked.refreshLLMManagedConnectionInventory).not.toHaveBeenCalled();
    expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    expect(invalidateQueries).toHaveBeenCalledTimes(1);
  });

  it("uses prior refs when response refs are absent", async () => {
    const priorConnectionRef: LLMConnectionRef = {
      connection_id: "55555555-5555-4555-8555-555555555555",
      expected_revision: 6,
    };
    const priorDeploymentRef: LLMDeploymentRef = {
      deployment_id: "66666666-6666-4666-8666-666666666666",
      expected_revision: 7,
    };
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef: null,
      deploymentRef: null,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("passed"));
    mocked.enableLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef: null,
      deploymentRef: null,
      runnable: true,
    }));

    const { result } = renderController({
      connection: {
        ...baseConnection,
        connectionRef: priorConnectionRef,
        deploymentRef: priorDeploymentRef,
        runnability: {
          status: "deployment_missing",
          selectable: true,
          runnable: false,
          reason: "Deployment model registration is required.",
        },
      },
    });

    expect(result.current.connectionRef).toEqual(priorConnectionRef);

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(mocked.testLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          api_key: "sk-controller-placeholder",
          connection_ref: priorConnectionRef,
        },
      );
      expect(mocked.enableLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          connection_ref: priorConnectionRef,
          deployment_ref: priorDeploymentRef,
        },
      );
    });
    expect(result.current.connectionRef).toEqual(priorConnectionRef);
    expect(result.current.runnable).toBe(true);
  });

  it("does not refresh or enable when verification fails with an effective deployment", async () => {
    const onSuccess = vi.fn();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("failed"));

    const { invalidateQueries, result } = renderController({ onSuccess });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith(
        "Ollama-compatible HTTPS endpoint connected",
        "The connection was saved, but GPT-OSS 20B is not ready yet.",
      );
    });
    expect(result.current.connectionRef).toEqual(connectionRef);
    expect(result.current.runnable).toBe(false);
    expect(mocked.refreshLLMManagedConnectionInventory).not.toHaveBeenCalled();
    expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expectCallOrder(
      mocked.createLLMManagedConnection,
      mocked.testLLMManagedConnection,
    );
  });

  it("refreshes and enables when passed verification fills a missing deployment", async () => {
    const onSuccess = vi.fn();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef: null,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("passed"));
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue(connectionStatus({
      connectionRef: refreshedConnectionRef,
      deploymentRef: refreshedDeploymentRef,
      runnable: false,
    }));
    mocked.enableLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef: refreshedConnectionRef,
      deploymentRef: refreshedDeploymentRef,
      runnable: true,
    }));

    const { invalidateQueries, result } = renderController({ onSuccess });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(mocked.refreshLLMManagedConnectionInventory).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          api_key: "sk-controller-placeholder",
          connection_ref: connectionRef,
        },
      );
      expect(mocked.enableLLMManagedConnection).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          connection_ref: refreshedConnectionRef,
          deployment_ref: refreshedDeploymentRef,
        },
      );
      expect(onSuccess).toHaveBeenCalledWith(
        "Ollama-compatible HTTPS endpoint connected",
        "GPT-OSS 20B is ready.",
      );
    });
    expect(result.current.connectionRef).toEqual(refreshedConnectionRef);
    expect(result.current.runnable).toBe(true);
    expectCallOrder(
      mocked.createLLMManagedConnection,
      mocked.testLLMManagedConnection,
      mocked.refreshLLMManagedConnectionInventory,
      mocked.enableLLMManagedConnection,
    );
  });

  it("refreshes but does not enable when verification fails and deployment is absent", async () => {
    const onSuccess = vi.fn();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef: null,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("failed"));
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: false,
    }));

    const { invalidateQueries, result } = renderController({ onSuccess });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(mocked.refreshLLMManagedConnectionInventory).toHaveBeenCalled();
      expect(onSuccess).toHaveBeenCalledWith(
        "Ollama-compatible HTTPS endpoint connected",
        "The connection was saved, but GPT-OSS 20B is not ready yet.",
      );
    });
    expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    expect(result.current.runnable).toBe(false);
    expect(invalidateQueries).toHaveBeenCalledTimes(1);
    expectCallOrder(
      mocked.createLLMManagedConnection,
      mocked.testLLMManagedConnection,
      mocked.refreshLLMManagedConnectionInventory,
    );
  });

  it("does not enable when refresh still yields no deployment", async () => {
    const onSuccess = vi.fn();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef: null,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("passed"));
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef: null,
      runnable: false,
    }));

    const { invalidateQueries, result } = renderController({ onSuccess });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(mocked.refreshLLMManagedConnectionInventory).toHaveBeenCalledWith(
        "ollama_openai_compatible_chat",
        {
          api_key: "sk-controller-placeholder",
          connection_ref: connectionRef,
        },
      );
      expect(onSuccess).toHaveBeenCalledWith(
        "Ollama-compatible HTTPS endpoint connected",
        "The connection was saved, but GPT-OSS 20B is not ready yet.",
      );
    });
    expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    expect(result.current.connectionRef).toEqual(connectionRef);
    expect(result.current.runnable).toBe(false);
    expect(invalidateQueries).toHaveBeenCalledTimes(1);
  });

  it("applies status before awaiting invalidation and publishes success after invalidation", async () => {
    const onSuccess = vi.fn();
    const invalidateDeferred = deferred<void>();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("passed"));
    mocked.enableLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: true,
    }));

    const { invalidateQueries, result } = renderController({
      invalidateQueries: () => invalidateDeferred.promise,
      onSuccess,
    });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(invalidateQueries).toHaveBeenCalledTimes(1);
      expect(result.current.connectionRef).toEqual(connectionRef);
      expect(result.current.runnable).toBe(true);
    });
    expect(onSuccess).not.toHaveBeenCalled();

    await act(async () => {
      invalidateDeferred.resolve();
      await invalidateDeferred.promise;
    });

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith(
        "Ollama-compatible HTTPS endpoint connected",
        "GPT-OSS 20B is ready.",
      );
    });
  });

  it.each([
    "create",
    "test",
    "refresh",
    "enable",
  ] as const)("short-circuits rejected %s helper without invalidation or success", async (stage) => {
    const onSuccess = vi.fn();
    const onError = vi.fn();
    mocked.createLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef: stage === "refresh" ? null : deploymentRef,
      runnable: false,
    }));
    mocked.testLLMManagedConnection.mockResolvedValue(verification("passed"));
    mocked.refreshLLMManagedConnectionInventory.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: false,
    }));
    mocked.enableLLMManagedConnection.mockResolvedValue(connectionStatus({
      connectionRef,
      deploymentRef,
      runnable: true,
    }));
    rejectStage(stage);

    const { invalidateQueries, result } = renderController({ onSuccess, onError });

    act(() => { result.current.connect(); });

    await waitFor(() => {
      expect(onError).toHaveBeenCalledTimes(1);
    });
    expect(onError.mock.calls[0]?.[0]).toBe(
      "Ollama-compatible HTTPS endpoint connection failed",
    );
    expect(onError.mock.calls[0]?.[1]).toBeInstanceOf(Error);
    expect(onError.mock.calls[0]?.[1].message).toBe(`${stage} failed`);
    expect(onSuccess).not.toHaveBeenCalled();
    expect(invalidateQueries).not.toHaveBeenCalled();

    if (stage === "create") {
      expect(mocked.testLLMManagedConnection).not.toHaveBeenCalled();
      expect(mocked.refreshLLMManagedConnectionInventory).not.toHaveBeenCalled();
      expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    }
    if (stage === "test") {
      expect(mocked.refreshLLMManagedConnectionInventory).not.toHaveBeenCalled();
      expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    }
    if (stage === "refresh") {
      expect(mocked.enableLLMManagedConnection).not.toHaveBeenCalled();
    }
  });
});

function renderController({
  connection = baseConnection,
  fieldValues = defaultFieldValues,
  invalidateQueries = async () => undefined,
  model = baseModel,
  onError = vi.fn(),
  onSuccess = vi.fn(),
}: {
  connection?: LLMConnectionMetadata;
  fieldValues?: Readonly<Record<string, string>>;
  invalidateQueries?: () => Promise<void>;
  model?: LLMCatalogModel;
  onError?: (title: string, error: Error) => void;
  onSuccess?: (title: string, description: string) => void;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const invalidateSpy = vi
    .spyOn(queryClient, "invalidateQueries")
    .mockImplementation(invalidateQueries);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  );
  const rendered = renderHook(
    () => useConnectionSettingsController({
      model,
      connection,
      fieldValues,
      onSuccess,
      onError,
    }),
    { wrapper },
  );
  return {
    ...rendered,
    invalidateQueries: invalidateSpy,
    queryClient,
  };
}

function connectionStatus({
  connectionRef: nextConnectionRef = connectionRef,
  deploymentRef: nextDeploymentRef = deploymentRef,
  runnable = false,
}: {
  connectionRef?: LLMConnectionRef | null;
  deploymentRef?: LLMDeploymentRef | null;
  runnable?: boolean;
}): LLMProvingConnectionStatus {
  return {
    lifecycleState: runnable ? "enabled" : "draft",
    connectionRef: nextConnectionRef,
    deploymentRef: nextDeploymentRef,
    verification: null,
    runnability: {
      status: runnable ? "runnable" : "capability_unknown",
      selectable: true,
      runnable,
      reason: runnable ? null : "Usage evidence is required.",
    },
  };
}

function verification(status: "failed" | "passed"): LLMProvingVerification {
  return {
    status,
    code: status === "passed" ? "verified" : "auth_failed",
    message: status === "passed"
      ? "Connection verified."
      : "Connection rejected the test key.",
    retryable: false,
  };
}

function expectCallOrder(...calls: Array<{ mock: { invocationCallOrder: number[] } }>) {
  for (let index = 1; index < calls.length; index += 1) {
    expect(calls[index - 1].mock.invocationCallOrder[0]).toBeLessThan(
      calls[index].mock.invocationCallOrder[0],
    );
  }
}

function rejectStage(stage: "create" | "enable" | "refresh" | "test") {
  if (stage === "create") {
    mocked.createLLMManagedConnection.mockRejectedValue("create failed");
  }
  if (stage === "test") {
    mocked.testLLMManagedConnection.mockRejectedValue("test failed");
  }
  if (stage === "refresh") {
    mocked.refreshLLMManagedConnectionInventory.mockRejectedValue("refresh failed");
  }
  if (stage === "enable") {
    mocked.enableLLMManagedConnection.mockRejectedValue("enable failed");
  }
}

function deferred<T>() {
  let resolve: (value: T | PromiseLike<T>) => void = () => undefined;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}
