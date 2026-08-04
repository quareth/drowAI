// @vitest-environment jsdom
/**
 * Tests for approval-resume mutation delivery.
 *
 * Approval requests claim one durable interrupt ticket and therefore must not
 * be replayed automatically after an HTTP conflict or ambiguous failure.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useGraphResume } from "@/hooks/useGraphResume";

const mocked = vi.hoisted(() => ({
  apiRequestMock: vi.fn(),
}));

vi.mock("@/lib/queryClient", () => ({
  apiRequest: mocked.apiRequestMock,
}));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: 1, retryDelay: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  mocked.apiRequestMock.mockReset();
});

describe("useGraphResume", () => {
  it("does not retry a ticket-claiming approval mutation", async () => {
    mocked.apiRequestMock.mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "Resume already in flight for this interrupt." }),
        {
          status: 409,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    const { result } = renderHook(() => useGraphResume(), { wrapper });
    result.current.mutate({
      taskId: 32,
      interruptType: "tool_approval",
      interruptId: "intr-parent",
      graphName: "parent_handoff",
      response: { action: "approve" },
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });

    expect(mocked.apiRequestMock).toHaveBeenCalledTimes(1);
  });
});
