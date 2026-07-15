/**
 * Tests for reusable navbar destination search ranking and grouping.
 */
import { describe, expect, it } from "vitest";

import { getAppSearchDestinations } from "@/navigation/registry";
import { groupSearchMatches, searchDestinations } from "@/navigation/search";

const destinations = getAppSearchDestinations({ permissions: new Set() });

function firstResultId(query: string): string | undefined {
  return searchDestinations(destinations, query)[0]?.destination.id;
}

function resultIds(query: string): string[] {
  return searchDestinations(destinations, query).map((match) => match.destination.id);
}

describe("destination search", () => {
  it("returns no matches for empty queries", () => {
    expect(searchDestinations(destinations, "")).toEqual([]);
    expect(searchDestinations(destinations, "   ")).toEqual([]);
  });

  it("ranks exact settings keyword matches first", () => {
    expect(firstResultId("api")).toBe("settings.section.api");
    expect(firstResultId("display")).toBe("settings.section.display");
    expect(firstResultId("language")).toBe("settings.section.display");
    expect(firstResultId("timezone")).toBe("settings.section.display");
  });

  it("finds knowledge destinations by security-domain synonyms", () => {
    expect(resultIds("asset")).toContain("knowledge.tab.assets");
    expect(resultIds("ip")).toContain("knowledge.tab.assets");
    expect(resultIds("hostname")).toContain("knowledge.tab.assets");
    expect(resultIds("network")).toContain("knowledge.tab.map");
  });

  it("finds report destinations", () => {
    expect(resultIds("report")).toEqual(
      expect.arrayContaining(["reports", "reports.tab.engagement"]),
    );
  });

  it("groups matches by destination group", () => {
    const groups = groupSearchMatches(searchDestinations(destinations, "report"));

    expect(groups.map((group) => group.group)).toContain("Reports");
  });
});
