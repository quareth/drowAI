// @vitest-environment jsdom
/**
 * Verifies raw tool-output hook behavior when using single-request batch lookup.
 *
 * Coverage includes state mapping, cache reuse, and disabled/identifier gating.
 */
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resetToolRawOutputCacheForTests, useToolRawOutput } from "@/components/chat/tool-card-terminal/useToolRawOutput";

const mocked = vi.hoisted(() => ({
  apiRequestMock: vi.fn(),
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequestMock,
}));

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  } as Response;
}

function textResponse(status: number, text: string): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => null,
    text: async () => text,
  } as Response;
}

afterEach(() => {
  cleanup();
  mocked.apiRequestMock.mockReset();
  resetToolRawOutputCacheForTests();
});

describe("useToolRawOutput", () => {
  it("returns idle when disabled and does not fetch", async () => {
    const { result } = renderHook(() =>
      useToolRawOutput({ taskId: 11, toolCallId: "call-idle", enabled: false }),
    );

    await waitFor(() => {
      expect(result.current.status).toBe("idle");
    });
    expect(mocked.apiRequestMock).not.toHaveBeenCalled();
  });

  it("returns missing_identifiers when enabled without task/tool identifiers", async () => {
    const { result } = renderHook(() =>
      useToolRawOutput({ taskId: null, toolCallId: "", enabled: true }),
    );

    await waitFor(() => {
      expect(result.current.status).toBe("not_available");
    });
    expect(result.current.state).toMatchObject({
      status: "not_available",
      reason: "missing_identifiers",
    });
    expect(mocked.apiRequestMock).not.toHaveBeenCalled();
  });

  it("maps batch ready payload to ready state", async () => {
    mocked.apiRequestMock.mockResolvedValueOnce(
      jsonResponse(200, {
        results: {
          "call-ready": {
            status: "ready",
            output_text: "$ ls\nfile.txt\n",
            command_artifact_id: "a1",
            stdout_artifact_id: "a2",
            stderr_artifact_id: null,
          },
        },
        missing: [],
      }),
    );

    const { result } = renderHook(() =>
      useToolRawOutput({ taskId: 9, toolCallId: "call-ready", enabled: true }),
    );

    await waitFor(() => {
      expect(result.current.status).toBe("ready");
    });
    expect(result.current.state).toMatchObject({
      status: "ready",
      outputText: "$ ls\nfile.txt\n",
      commandArtifactId: "a1",
      stdoutArtifactId: "a2",
    });
    expect(mocked.apiRequestMock).toHaveBeenCalledTimes(1);
  });

  it("maps missing execution to execution_not_found", async () => {
    mocked.apiRequestMock.mockResolvedValueOnce(
      jsonResponse(200, {
        results: {},
        missing: ["call-missing"],
      }),
    );

    const { result } = renderHook(() =>
      useToolRawOutput({ taskId: 9, toolCallId: "call-missing", enabled: true }),
    );

    await waitFor(() => {
      expect(result.current.status).toBe("not_available");
    });
    expect(result.current.state).toMatchObject({
      status: "not_available",
      reason: "execution_not_found",
    });
    expect(mocked.apiRequestMock).toHaveBeenCalledTimes(1);
  });

  it("maps missing_output_artifacts from batch response", async () => {
    mocked.apiRequestMock.mockResolvedValueOnce(
      jsonResponse(200, {
        results: {
          "call-no-artifacts": {
            status: "not_available",
            reason: "missing_output_artifacts",
            command_artifact_id: null,
            stdout_artifact_id: null,
            stderr_artifact_id: null,
          },
        },
        missing: [],
      }),
    );

    const { result } = renderHook(() =>
      useToolRawOutput({ taskId: 19, toolCallId: "call-no-artifacts", enabled: true }),
    );

    await waitFor(() => {
      expect(result.current.status).toBe("not_available");
    });
    expect(result.current.state).toMatchObject({
      status: "not_available",
      reason: "missing_output_artifacts",
    });
  });

  it("maps artifact_content_unavailable from batch response", async () => {
    mocked.apiRequestMock.mockResolvedValueOnce(
      jsonResponse(200, {
        results: {
          "call-omitted": {
            status: "not_available",
            reason: "artifact_content_unavailable",
            stdout_artifact_id: "stdout-x",
          },
        },
        missing: [],
      }),
    );

    const { result } = renderHook(() =>
      useToolRawOutput({ taskId: 20, toolCallId: "call-omitted", enabled: true }),
    );

    await waitFor(() => {
      expect(result.current.status).toBe("not_available");
    });
    expect(result.current.state).toMatchObject({
      status: "not_available",
      reason: "artifact_content_unavailable",
      stdoutArtifactId: "stdout-x",
    });
  });

  it("returns error state when batch lookup fails", async () => {
    mocked.apiRequestMock.mockResolvedValueOnce(textResponse(500, "batch failed"));

    const { result } = renderHook(() =>
      useToolRawOutput({ taskId: 44, toolCallId: "call-fail", enabled: true }),
    );

    await waitFor(() => {
      expect(result.current.status).toBe("error");
    });
    expect(result.current.state).toMatchObject({
      status: "error",
      message: "batch failed",
    });
  });

  it("uses cache for repeated same task/tool_call_id without duplicate fetches", async () => {
    mocked.apiRequestMock.mockResolvedValueOnce(
      jsonResponse(200, {
        results: {
          "call-cache": {
            status: "ready",
            output_text: "cached output\n",
            stdout_artifact_id: "stdout-2",
          },
        },
        missing: [],
      }),
    );

    const first = renderHook(() =>
      useToolRawOutput({ taskId: 18, toolCallId: "call-cache", enabled: true }),
    );
    await waitFor(() => {
      expect(first.result.current.status).toBe("ready");
    });
    expect(mocked.apiRequestMock).toHaveBeenCalledTimes(1);
    first.unmount();

    const second = renderHook(() =>
      useToolRawOutput({ taskId: 18, toolCallId: "call-cache", enabled: true }),
    );
    await waitFor(() => {
      expect(second.result.current.status).toBe("ready");
    });
    expect(mocked.apiRequestMock).toHaveBeenCalledTimes(1);
  });
});
