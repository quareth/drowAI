// @vitest-environment jsdom
/**
 * Verifies AuthProvider configures proactive /api/auth/me permission refresh.
 */

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/hooks/use-auth";

const mocked = vi.hoisted(() => ({
  useQuery: vi.fn(),
  useMutation: vi.fn(),
}));
const authMocks = vi.hoisted(() => ({
  clearAccessToken: vi.fn(),
  getAccessToken: vi.fn(() => "access-token"),
  getAccessTokenExpiresAtMs: vi.fn(() => null as number | null),
  registerAuthRecoveryHandler: vi.fn(),
  setAccessToken: vi.fn(),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: mocked.useQuery,
  useMutation: mocked.useMutation,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/lib/auth-session", () => ({
  ACCESS_TOKEN_REFRESH_SKEW_MS: 60_000,
  addAccessTokenChangeListener: vi.fn(() => vi.fn()),
  clearAccessToken: authMocks.clearAccessToken,
  getAccessToken: authMocks.getAccessToken,
  getAccessTokenExpiresAtMs: authMocks.getAccessTokenExpiresAtMs,
  registerAuthRecoveryHandler: authMocks.registerAuthRecoveryHandler,
  setAccessToken: authMocks.setAccessToken,
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: vi.fn(),
  getQueryFn: vi.fn(() => vi.fn()),
  queryClient: {
    setQueryData: vi.fn(),
    clear: vi.fn(),
    invalidateQueries: vi.fn(),
  },
}));

describe("AuthProvider permission refresh", () => {
  beforeEach(() => {
    vi.useRealTimers();
    authMocks.clearAccessToken.mockReset();
    authMocks.getAccessToken.mockReset();
    authMocks.getAccessToken.mockReturnValue("access-token");
    authMocks.getAccessTokenExpiresAtMs.mockReset();
    authMocks.getAccessTokenExpiresAtMs.mockReturnValue(null);
    authMocks.registerAuthRecoveryHandler.mockReset();
    authMocks.setAccessToken.mockReset();
    mocked.useMutation.mockReturnValue({
      mutate: vi.fn(),
      mutateAsync: vi.fn(),
      isPending: false,
    });
    mocked.useQuery.mockReturnValue({
      data: null,
      error: null,
      isLoading: false,
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("revalidates /api/auth/me on focus and a short interval", () => {
    render(
      <AuthProvider>
        <div>child</div>
      </AuthProvider>,
    );

    const useQueryArgs = mocked.useQuery.mock.calls.find(
      (args) => args?.[0]?.queryKey?.[0] === "/api/auth/me",
    )?.[0];

    expect(useQueryArgs).toBeDefined();
    expect(useQueryArgs.refetchOnWindowFocus).toBe(true);
    expect(useQueryArgs.staleTime).toBe(0);
    expect(useQueryArgs.refetchInterval).toBe(60_000);
  });

  it("refreshes with cookies shortly before the access token expires", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-27T10:00:00Z"));
    authMocks.getAccessTokenExpiresAtMs.mockReturnValue(Date.now() + 120_000);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ access_token: "refreshed-token" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 1, username: "alice" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AuthProvider>
        <div>child</div>
      </AuthProvider>,
    );

    await vi.advanceTimersByTimeAsync(60_000);
    await Promise.resolve();
    await Promise.resolve();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/auth/refresh",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
      }),
    );
    expect(authMocks.setAccessToken).toHaveBeenCalledWith("refreshed-token");
  });
});
