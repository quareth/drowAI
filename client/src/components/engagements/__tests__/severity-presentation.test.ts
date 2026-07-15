import { describe, expect, it } from "vitest";

import {
  formatFindingStatusLabel,
  normalizeFindingStatus,
  resolveFindingDisplayStatus,
} from "@/components/engagements/finding-presentation";
import {
  compareSeverity,
  FINDING_SEVERITY_FILTER_OPTIONS,
  formatSeverityLabel,
  normalizeSeverity,
  severityBadgeClass,
  severityIndicatorTone,
  severityRank,
  severityTone,
} from "@/components/engagements/severity-presentation";

describe("severity-presentation", () => {
  it("normalizes null, blank, mixed-case, and whitespace severity values", () => {
    expect(normalizeSeverity(null)).toBe("unknown");
    expect(normalizeSeverity("")).toBe("unknown");
    expect(normalizeSeverity("  HIGH ")).toBe("high");
    expect(normalizeSeverity(" Medium ")).toBe("medium");
  });

  it("ranks severities deterministically with unknown last", () => {
    expect(["unknown", "medium", "critical", "low", "high", "info"].sort(compareSeverity)).toEqual([
      "critical",
      "high",
      "medium",
      "low",
      "info",
      "unknown",
    ]);
    expect(severityRank("unexpected")).toBe(severityRank("unknown"));
  });

  it("exposes backend-supported severity filter options without unknown", () => {
    expect(FINDING_SEVERITY_FILTER_OPTIONS).toEqual([
      { value: "critical", label: "Critical" },
      { value: "high", label: "High" },
      { value: "medium", label: "Medium" },
      { value: "low", label: "Low" },
      { value: "info", label: "Info" },
    ]);
  });

  it("resolves canonical badge classes and labels", () => {
    expect(severityBadgeClass("critical")).toContain("border-red");
    expect(severityBadgeClass("high")).toContain("border-orange");
    expect(severityBadgeClass("medium")).toContain("border-amber");
    expect(severityBadgeClass("low")).toContain("border-cyan");
    expect(severityBadgeClass("info")).toContain("border-slate");
    expect(severityBadgeClass("unexpected")).toContain("border-slate");
    expect(severityIndicatorTone("critical")).toBe("severityCritical");
    expect(severityIndicatorTone("unexpected")).toBe("neutral");
    expect(severityTone("unexpected")).toBe("unknown");
    expect(formatSeverityLabel(" credential exposure ")).toBe("Credential exposure");
  });
});

describe("finding-presentation", () => {
  it("normalizes null, blank, mixed-case, and whitespace status values", () => {
    expect(normalizeFindingStatus(null)).toBe("unknown");
    expect(normalizeFindingStatus("")).toBe("unknown");
    expect(normalizeFindingStatus("  EXPLOITED ")).toBe("exploited");
    expect(normalizeFindingStatus(" Open ")).toBe("open");
  });

  it("formats status labels across spaces, underscores, and hyphens", () => {
    expect(formatFindingStatusLabel(null)).toBe("Unknown");
    expect(formatFindingStatusLabel("open")).toBe("Open");
    expect(formatFindingStatusLabel("pending_review")).toBe("Pending Review");
    expect(formatFindingStatusLabel("false-positive")).toBe("False Positive");
    expect(formatFindingStatusLabel("needs analyst review")).toBe("Needs Analyst Review");
  });

  it("lets exploited projection state override raw status for display", () => {
    expect(resolveFindingDisplayStatus({ status: "open", is_exploited: true })).toBe("exploited");
    expect(resolveFindingDisplayStatus({ status: "confirmed", isExploited: true })).toBe("exploited");
    expect(resolveFindingDisplayStatus({ status: "open", is_exploited: false })).toBe("open");
  });
});
