// @vitest-environment jsdom
/* Unit tests for <UsageRecordsTable />.
 *
 * Focus:
 *  - Next/Prev pagination: Next calls the hook with page=2 on click; Prev is
 *    disabled on page 1; Next is disabled when has_more is false.
 *  - Cache-reporting formatting is honest (reported / not reported / unknown).
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UsageRecordsTable } from "@/components/task/usage-insights/usage-records-table";
import type {
  CacheReporting,
  UsageInsightsRecord,
  UsageInsightsRecordsResponse,
} from "@/types/usage";

// Capture the arguments passed to useUsageInsightsRecords so we can assert
// pagination behavior without rendering through react-query's real machinery.
const recordsCalls: Array<{
  taskId: number | null | undefined;
  page: number;
  pageSize: number;
  filters: unknown;
}> = [];

const recordsReturn = {
  data: undefined as UsageInsightsRecordsResponse | undefined,
  isLoading: false,
  isError: false,
  error: null as unknown,
};

vi.mock("@/hooks/useUsageInsights", () => ({
  useUsageInsightsOverview: () => recordsReturn,
  useUsageInsightsGroups: () => recordsReturn,
  useUsageInsightsTimeline: () => recordsReturn,
  useUsageInsightsRecords: (
    taskId: number | null | undefined,
    page: number,
    pageSize: number,
    filters: unknown,
  ) => {
    recordsCalls.push({ taskId, page, pageSize, filters });
    return recordsReturn;
  },
}));

function makeRecord(
  overrides: Partial<UsageInsightsRecord> = {},
): UsageInsightsRecord {
  return {
    id: 1,
    created_at: "2026-04-14T12:00:00.000Z",
    model: "gpt-5",
    source: "langgraph",
    conversation_id: null,
    prompt_tokens: 100,
    completion_tokens: 50,
    total_tokens: 150,
    cached_tokens: 20,
    reasoning_tokens: 0,
    cost_usd: 0.015,
    role: "planner",
    node_name: "plan_node",
    execution_branch: "main",
    provider: "openai",
    api_surface: "responses",
    request_mode: "sync",
    cache_reporting: "reported",
    turn_index: 0,
    ...overrides,
  };
}

function makeResponse(
  items: UsageInsightsRecord[],
  partial: Partial<UsageInsightsRecordsResponse> = {},
): UsageInsightsRecordsResponse {
  return {
    task_id: 7,
    items,
    total_count: items.length,
    page: 1,
    page_size: 25,
    has_more: false,
    ...partial,
  };
}

beforeEach(() => {
  recordsCalls.length = 0;
  recordsReturn.data = undefined;
  recordsReturn.isLoading = false;
  recordsReturn.isError = false;
  recordsReturn.error = null;
});

afterEach(() => {
  // Project's vitest setup does not auto-cleanup between tests, so each test
  // must explicitly unmount to avoid the previous render bleeding into `screen`.
  cleanup();
});

describe("<UsageRecordsTable /> pagination", () => {
  it("requests page=1 on initial render with page_size=25", () => {
    recordsReturn.data = makeResponse([makeRecord()], {
      page: 1,
      has_more: true,
      total_count: 100,
    });

    render(<UsageRecordsTable taskId={7} filters={{}} />);

    expect(recordsCalls.length).toBeGreaterThan(0);
    const firstCall = recordsCalls[0];
    expect(firstCall.taskId).toBe(7);
    expect(firstCall.page).toBe(1);
    expect(firstCall.pageSize).toBe(25);
  });

  it("disables Next when has_more is false", () => {
    recordsReturn.data = makeResponse([makeRecord()], {
      page: 1,
      has_more: false,
      total_count: 1,
    });

    render(<UsageRecordsTable taskId={7} filters={{}} />);

    const next = screen.getByTestId("usage-records-next") as HTMLButtonElement;
    expect(next.disabled).toBe(true);

    // Prev is also disabled on page 1.
    const prev = screen.getByTestId("usage-records-prev") as HTMLButtonElement;
    expect(prev.disabled).toBe(true);
  });

  it("advances to page=2 when Next is clicked and has_more=true", () => {
    recordsReturn.data = makeResponse([makeRecord()], {
      page: 1,
      has_more: true,
      total_count: 100,
    });

    render(<UsageRecordsTable taskId={7} filters={{}} />);

    recordsCalls.length = 0; // Reset to isolate the post-click call set.

    const next = screen.getByTestId("usage-records-next") as HTMLButtonElement;
    expect(next.disabled).toBe(false);
    fireEvent.click(next);

    // After the click, the hook should be re-invoked with page=2.
    const lastCall = recordsCalls[recordsCalls.length - 1];
    expect(lastCall.page).toBe(2);
    expect(lastCall.pageSize).toBe(25);
  });
});

describe("<UsageRecordsTable /> cache_reporting formatting", () => {
  it("renders 'Reported', 'Not reported', and 'Unknown' distinctly per row", () => {
    const statuses: CacheReporting[] = ["reported", "not_reported", "unknown"];
    const records = statuses.map((status, idx) =>
      makeRecord({ id: idx + 100, cache_reporting: status }),
    );
    recordsReturn.data = makeResponse(records, {
      page: 1,
      has_more: false,
      total_count: records.length,
    });

    render(<UsageRecordsTable taskId={7} filters={{}} />);

    const reportedBadge = screen.getByTestId("usage-record-cache-100");
    const notReportedBadge = screen.getByTestId("usage-record-cache-101");
    const unknownBadge = screen.getByTestId("usage-record-cache-102");

    expect(reportedBadge.textContent ?? "").toMatch(/^Reported$/);
    expect(notReportedBadge.textContent ?? "").toMatch(/^Not reported$/);
    expect(unknownBadge.textContent ?? "").toMatch(/^Unknown$/);
  });
});
