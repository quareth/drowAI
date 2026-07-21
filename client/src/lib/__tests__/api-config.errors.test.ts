// @vitest-environment jsdom
/**
 * Verifies that shared API calls expose backend-safe error details to UI consumers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiCall } from "@/lib/api-config";

describe("apiCall error handling", () => {
  beforeEach(() => {
    const storage = {
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    vi.stubGlobal("localStorage", storage);
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: storage,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("uses a FastAPI detail message instead of rendering the raw JSON body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "Invalid Hugging Face API key" }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      },
    )));

    await expect(apiCall("/api/llm/connection/test", { method: "POST" }))
      .rejects.toMatchObject({ message: "Invalid Hugging Face API key" });
  });
});
