// @vitest-environment jsdom
/**
 * Verifies apiFetch abort contracts:
 * - caller-provided AbortSignal propagates to the underlying fetch request
 * - timeout-based abort still terminates requests when caller signal is absent
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiConfig, apiFetch } from "@/lib/api-config";

function createAbortableFetchMock() {
  return vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    return new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) {
        reject(new Error("Expected request signal"));
        return;
      }

      const rejectWithAbort = () => {
        reject(new DOMException("The operation was aborted", "AbortError"));
      };

      if (signal.aborted) {
        rejectWithAbort();
        return;
      }

      signal.addEventListener("abort", rejectWithAbort, { once: true });
    });
  });
}

describe("apiFetch abort handling", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("propagates caller abort signal to fetch", async () => {
    const fetchMock = createAbortableFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    const callerController = new AbortController();
    const requestPromise = apiFetch("/api/test", { signal: callerController.signal });
    callerController.abort();

    await expect(requestPromise).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, requestOptions] = fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit];
    expect(requestOptions.signal).toBeDefined();
    expect(requestOptions.signal?.aborted).toBe(true);
  });

  it("aborts on timeout when no caller signal is provided", async () => {
    vi.useFakeTimers();
    const fetchMock = createAbortableFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    const requestPromise = apiFetch("/api/test");
    const assertion = expect(requestPromise).rejects.toThrow(`Request timeout after ${apiConfig.timeout}ms`);
    await vi.advanceTimersByTimeAsync(apiConfig.timeout + 1);

    await assertion;
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
