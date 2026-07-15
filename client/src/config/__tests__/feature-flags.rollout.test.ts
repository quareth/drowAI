/**
 * Verifies deterministic task-based percentage rollout bucketing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isTaskInPercentageRollout } from "@/config/feature-flags";

beforeEach(() => {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem("featureFlags");
  }
});

afterEach(() => {
  vi.resetModules();
});

describe("isTaskInPercentageRollout", () => {
  it("returns false for invalid task ids or 0 percent", () => {
    expect(isTaskInPercentageRollout(null, 10)).toBe(false);
    expect(isTaskInPercentageRollout(undefined, 10)).toBe(false);
    expect(isTaskInPercentageRollout(0, 10)).toBe(false);
    expect(isTaskInPercentageRollout(123, 0)).toBe(false);
  });

  it("uses deterministic task modulo bucketing", () => {
    expect(isTaskInPercentageRollout(101, 2)).toBe(true); // 101 % 100 = 1
    expect(isTaskInPercentageRollout(109, 2)).toBe(false); // 109 % 100 = 9
  });

  it("clamps out-of-range percent values", () => {
    expect(isTaskInPercentageRollout(501, -5)).toBe(false);
    expect(isTaskInPercentageRollout(501, 101)).toBe(true);
  });
});

