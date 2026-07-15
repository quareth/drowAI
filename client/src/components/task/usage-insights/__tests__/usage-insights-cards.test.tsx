// @vitest-environment jsdom
/* Unit tests for <UsageInsightsCards />.
 *
 * Focus:
 *  - Renders skeleton placeholders while loading.
 *  - Renders the "no LLM calls" empty state when the backend returns call_count=0.
 *  - Renders all six cards and exposes the partial-coverage badge only when
 *    cache_reporting_coverage < 1.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";

import { UsageInsightsCards } from "@/components/task/usage-insights/usage-insights-cards";
import type { UsageInsightsOverviewResponse } from "@/types/usage";

// Hook mock — the component reads every number through
// `useUsageInsightsOverview`, so mocking this one symbol captures the data flow.
const hookState = { result: { data: undefined, isLoading: false, isError: false, error: null } as Record<string, unknown> };

vi.mock("@/hooks/useUsageInsights", () => ({
  useUsageInsightsOverview: () => hookState.result,
  // Unused but present so any barrel imports don't blow up.
  useUsageInsightsGroups: () => hookState.result,
  useUsageInsightsTimeline: () => hookState.result,
  useUsageInsightsRecords: () => hookState.result,
}));

beforeEach(() => {
  hookState.result = {
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
  };
});

afterEach(() => {
  // Project's vitest setup does not auto-cleanup between tests, so each test
  // must explicitly unmount to avoid the previous render bleeding into `screen`.
  cleanup();
});

const FULL_COVERAGE: UsageInsightsOverviewResponse = {
  task_id: 7,
  provider_coverage: { openai: 10 },
  call_count: 10,
  prompt_tokens: 4000,
  completion_tokens: 1000,
  cached_tokens: 1200,
  uncached_prompt_tokens: 2800,
  cache_hit_calls: 7,
  cache_hit_rate: 0.7,
  cache_ratio: 0.3,
  cache_reporting_call_count: 10,
  cache_reporting_coverage: 1.0,
  cost_usd: 2.4567,
  cached_input_cost_usd: 0.2,
  uncached_input_cost_usd: 1.5,
  output_cost_usd: 0.75,
};

const PARTIAL_COVERAGE: UsageInsightsOverviewResponse = {
  ...FULL_COVERAGE,
  cache_reporting_call_count: 6,
  cache_reporting_coverage: 0.6,
};

describe("<UsageInsightsCards />", () => {
  it("renders skeletons while the hook is loading", () => {
    hookState.result = {
      data: undefined,
      isLoading: true,
      isError: false,
      error: null,
    };

    render(<UsageInsightsCards taskId={7} filters={{}} />);

    // All six skeleton placeholders should appear (two skeleton blocks per card).
    const cards = screen.getAllByText(
      /total tokens|total cost|cached tokens|cache hit rate|uncached prompt tokens|call count/i,
    );
    expect(cards.length).toBeGreaterThanOrEqual(6);
    // The skeleton render path MUST NOT show any numeric values.
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it("renders an empty state when the task has no LLM calls", () => {
    hookState.result = {
      data: { ...FULL_COVERAGE, call_count: 0 },
      isLoading: false,
      isError: false,
      error: null,
    };

    render(<UsageInsightsCards taskId={7} filters={{}} />);
    expect(
      screen.getByText(/no llm calls recorded for this task yet/i),
    ).toBeTruthy();
  });

  it("renders all six cards with backend-provided values and no partial-coverage badge at 100% coverage", () => {
    hookState.result = {
      data: FULL_COVERAGE,
      isLoading: false,
      isError: false,
      error: null,
    };

    render(<UsageInsightsCards taskId={7} filters={{}} />);

    // Labels for every card.
    expect(screen.getByText("Total tokens")).toBeTruthy();
    expect(screen.getByText("Total cost")).toBeTruthy();
    expect(screen.getByText("Cached tokens")).toBeTruthy();
    expect(screen.getByText("Cache hit rate")).toBeTruthy();
    expect(screen.getByText("Uncached prompt tokens")).toBeTruthy();
    expect(screen.getByText("Call count")).toBeTruthy();

    // A handful of values we can trust to be formatted verbatim.
    // total cost uses 2 decimals for values >= $0.01.
    expect(screen.getByText("$2.46")).toBeTruthy();
    // cache hit rate is 70%.
    expect(screen.getByText("70.0%")).toBeTruthy();
    // call count shows the locale-formatted integer.
    expect(screen.getByText("10")).toBeTruthy();

    // No partial-coverage badge when coverage is 1.0.
    expect(screen.queryByTestId("usage-card-cache_hit_rate-badge")).toBeNull();
  });

  it("shows the partial-coverage badge when cache_reporting_coverage < 1", () => {
    hookState.result = {
      data: PARTIAL_COVERAGE,
      isLoading: false,
      isError: false,
      error: null,
    };

    render(<UsageInsightsCards taskId={7} filters={{}} />);

    const badge = screen.getByTestId("usage-card-cache_hit_rate-badge");
    expect(badge).toBeTruthy();
    expect(badge.textContent ?? "").toMatch(/partial coverage/i);
  });

  it("renders a disabled empty state when taskId is null", () => {
    render(<UsageInsightsCards taskId={null} filters={{}} />);
    expect(
      screen.getByText(/select a task to see usage insights/i),
    ).toBeTruthy();
  });
});
