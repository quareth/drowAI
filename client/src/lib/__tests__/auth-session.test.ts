// @vitest-environment jsdom
/**
 * Verifies centralized auth-session coordination:
 * - token storage helpers
 * - in-flight recovery deduplication
 * - final invalidation when recovery fails
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearAccessToken,
  getAccessTokenExpiresAtMs,
  getAccessToken,
  recoverSessionAfterAuthFailure,
  registerAuthRecoveryHandler,
  setAccessToken,
} from "@/lib/auth-session";

function buildJwt(payload: Record<string, unknown>): string {
  const encodedPayload = window
    .btoa(JSON.stringify(payload))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
  return `header.${encodedPayload}.signature`;
}

function installStorageMock(): void {
  const store = new Map<string, string>();
  const mockStorage = {
    getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => {
      store.clear();
    },
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: mockStorage,
  });
  vi.stubGlobal("localStorage", mockStorage);
}

describe("auth-session", () => {
  beforeEach(() => {
    installStorageMock();
    localStorage.clear();
    registerAuthRecoveryHandler(null);
    window.history.replaceState({}, "", "/auth");
  });

  afterEach(() => {
    registerAuthRecoveryHandler(null);
  });

  it("stores and clears access token through helper APIs", () => {
    expect(getAccessToken()).toBeNull();
    setAccessToken("token-1");
    expect(getAccessToken()).toBe("token-1");
    clearAccessToken();
    expect(getAccessToken()).toBeNull();
  });

  it("reads access token expiry from the JWT exp claim", () => {
    const token = buildJwt({ exp: 12345 });

    expect(getAccessTokenExpiresAtMs(token)).toBe(12_345_000);
    expect(getAccessTokenExpiresAtMs("not-a-jwt")).toBeNull();
  });

  it("deduplicates concurrent recovery attempts with a shared in-flight promise", async () => {
    let calls = 0;
    let release: ((value: boolean) => void) | null = null;
    const gate = new Promise<boolean>((resolve) => {
      release = resolve;
    });

    registerAuthRecoveryHandler(async () => {
      calls += 1;
      return gate;
    });

    const first = recoverSessionAfterAuthFailure({ source: "http_401" });
    const second = recoverSessionAfterAuthFailure({ source: "runtime_ws" });

    expect(calls).toBe(1);
    release?.(true);

    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(true);
    expect(calls).toBe(1);
  });

  it("invalidates local token when recovery handler returns false", async () => {
    setAccessToken("stale-token");
    registerAuthRecoveryHandler(async () => false);

    await expect(recoverSessionAfterAuthFailure({ source: "http_401" })).resolves.toBe(false);
    expect(getAccessToken()).toBeNull();
  });
});
