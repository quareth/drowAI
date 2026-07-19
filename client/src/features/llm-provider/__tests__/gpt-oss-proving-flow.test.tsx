/**
 * Verifies the mocked GPT-OSS proving API flow contract.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createLLMProvingConnection,
  enableLLMProvingConnection,
  saveLLMDeploymentSelection,
  testLLMProvingConnection,
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

describe("GPT-OSS proving API flow", () => {
  it("creates, tests, enables, and selects one deployment without arbitrary endpoint config", async () => {
    const connectionRef = {
      connection_id: "11111111-1111-4111-8111-111111111111",
      expected_revision: 2,
    };
    const deploymentRef = {
      deployment_id: "22222222-2222-4222-8222-222222222222",
      expected_revision: 1,
    };

    mocked.apiCall
      .mockResolvedValueOnce({
        lifecycle_state: "draft",
        connection_ref: connectionRef,
        deployment_ref: deploymentRef,
        verification: {
          status: "failed",
          code: "not_tested",
          message: "Verification has not run.",
          retryable: false,
        },
        runnability: {
          status: "capability_unknown",
          selectable: true,
          runnable: false,
          reason: "Usage evidence is required.",
        },
      })
      .mockResolvedValueOnce({
        status: "passed",
        code: "verified",
        message: "GPT-OSS proving endpoint verified",
        retryable: false,
        model_present: true,
        usage: {
          prompt_tokens: 5,
          completion_tokens: 2,
          total_tokens: 7,
        },
      })
      .mockResolvedValueOnce({
        lifecycle_state: "enabled",
        connection_ref: { ...connectionRef, expected_revision: 4 },
        deployment_ref: deploymentRef,
        runnability: {
          status: "runnable",
          selectable: true,
          runnable: true,
          reason: null,
        },
      })
      .mockResolvedValueOnce({
        provider: "openai",
        model: "gpt-oss-20b",
        deployment_ref: deploymentRef,
        selection_status: { status: "selectable", selectable: true, runnable: true },
      });

    await createLLMProvingConnection(
      "gpt_oss_20b_openai_compatible_proving",
      { api_key: "test-api-key" },
    );
    await testLLMProvingConnection(
      "gpt_oss_20b_openai_compatible_proving",
      {
        api_key: "test-api-key",
        connection_ref: connectionRef,
        deployment_ref: deploymentRef,
      },
    );
    await enableLLMProvingConnection(
      "gpt_oss_20b_openai_compatible_proving",
      {
        connection_ref: connectionRef,
        deployment_ref: deploymentRef,
      },
    );
    await saveLLMDeploymentSelection({ deployment_ref: deploymentRef });

    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      1,
      "/api/llm/proving-presets/gpt_oss_20b_openai_compatible_proving/connection",
      {
        method: "POST",
        body: JSON.stringify({ api_key: "test-api-key" }),
      },
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      2,
      "/api/llm/proving-presets/gpt_oss_20b_openai_compatible_proving/connection/test",
      {
        method: "POST",
        body: JSON.stringify({
          api_key: "test-api-key",
          connection_ref: connectionRef,
          deployment_ref: deploymentRef,
        }),
      },
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(
      3,
      "/api/llm/proving-presets/gpt_oss_20b_openai_compatible_proving/connection/enable",
      {
        method: "POST",
        body: JSON.stringify({
          connection_ref: connectionRef,
          deployment_ref: deploymentRef,
        }),
      },
    );
    expect(mocked.apiCall).toHaveBeenNthCalledWith(4, "/api/llm/selection", {
      method: "PUT",
      body: JSON.stringify({ deployment_ref: deploymentRef }),
    });

    for (const call of mocked.apiCall.mock.calls) {
      const options = call[1] as { body?: string } | undefined;
      expect(options?.body ?? "").not.toMatch(/endpoint|header|authorization/i);
    }
  });
});
