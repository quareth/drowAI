// @vitest-environment jsdom
/* Tests user-scoped knowledge hooks and API route construction. */

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useKnowledgeFinding } from "@/hooks/use-knowledge";

const mocked = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: mocked.apiFetch,
}));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  mocked.apiFetch.mockReset();
});

describe("use-knowledge", () => {
  it("encodes finding ids before using them as route segments", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "finding/with/slash", title: "Finding detail" }),
    } as Response);

    const { result } = renderHook(() => useKnowledgeFinding("finding/with/slash"), { wrapper });

    await waitFor(() => {
      expect(result.current.data?.id).toBe("finding/with/slash");
    });

    expect(mocked.apiFetch).toHaveBeenCalledWith(
      "/api/knowledge/findings/finding%2Fwith%2Fslash",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("does not request disabled finding details", async () => {
    renderHook(() => useKnowledgeFinding(null), { wrapper });

    await waitFor(() => {
      expect(mocked.apiFetch).not.toHaveBeenCalled();
    });
  });

  it("surfaces failed finding detail responses as query errors", async () => {
    mocked.apiFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Server error",
      text: async () => "detail failed",
    } as Response);

    const { result } = renderHook(() => useKnowledgeFinding("finding-1"), { wrapper });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error).toBeInstanceOf(Error);
  });
});
