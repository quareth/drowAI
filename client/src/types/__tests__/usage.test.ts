import { describe, expect, it } from "vitest";

import {
  formatCacheReportingLabel,
  formatCostUsd,
  formatRatio,
  type CacheReporting,
} from "@/types/usage";

describe("formatRatio", () => {
  it("renders a fractional ratio with one decimal percent", () => {
    expect(formatRatio(0.1234)).toBe("12.3%");
  });

  it("renders zero as '0.0%' for consistent one-decimal output", () => {
    // Convention (documented in usage.ts): zero uses one decimal so it reads
    // the same way as non-zero ratios in tables and tooltips.
    expect(formatRatio(0)).toBe("0.0%");
  });

  it("renders one as '100.0%'", () => {
    expect(formatRatio(1)).toBe("100.0%");
  });

  it("clamps values above 1 to 100%", () => {
    expect(formatRatio(1.25)).toBe("100.0%");
  });

  it("clamps negatives to 0%", () => {
    expect(formatRatio(-0.1)).toBe("0.0%");
  });

  it("falls back to 0.0% for non-finite inputs", () => {
    expect(formatRatio(Number.NaN)).toBe("0.0%");
    expect(formatRatio(Number.POSITIVE_INFINITY)).toBe("0.0%");
  });
});

describe("formatCostUsd", () => {
  it("renders whole-dollar-plus values with two decimals", () => {
    expect(formatCostUsd(1.82)).toBe("$1.82");
  });

  it("renders sub-cent positive values with four decimals", () => {
    expect(formatCostUsd(0.0042)).toBe("$0.0042");
  });

  it("renders zero as '$0.00'", () => {
    expect(formatCostUsd(0)).toBe("$0.00");
  });

  it("falls back to '$0.00' for non-finite inputs", () => {
    expect(formatCostUsd(Number.NaN)).toBe("$0.00");
    expect(formatCostUsd(Number.POSITIVE_INFINITY)).toBe("$0.00");
  });
});

describe("formatCacheReportingLabel", () => {
  it("maps 'reported' to 'Reported'", () => {
    const value: CacheReporting = "reported";
    expect(formatCacheReportingLabel(value)).toBe("Reported");
  });

  it("maps 'not_reported' to 'Not reported'", () => {
    const value: CacheReporting = "not_reported";
    expect(formatCacheReportingLabel(value)).toBe("Not reported");
  });

  it("maps 'unknown' to 'Unknown'", () => {
    const value: CacheReporting = "unknown";
    expect(formatCacheReportingLabel(value)).toBe("Unknown");
  });
});
