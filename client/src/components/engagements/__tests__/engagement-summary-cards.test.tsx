// @vitest-environment jsdom
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EngagementSummaryCards } from "@/components/engagements/engagement-summary-cards";
import { AuthContext } from "@/hooks/use-auth";

function renderWithAuth(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider
        value={{
          user: null,
          isLoading: false,
          error: null,
          loginMutation: {} as never,
          logoutMutation: {} as never,
          registerMutation: {} as never,
        }}
      >
        {ui}
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
}

describe("engagement-summary-cards", () => {
  it("renders summary values and severity badges", () => {
    renderWithAuth(
      <EngagementSummaryCards
        summary={{
          engagement_id: 7,
          open_findings_total: 6,
          open_findings_by_severity: {
            critical: 2,
            high: 3,
            low: 1,
          },
          asset_counts: { total: 10, vulnerable: 4, exploited: 1 },
          service_count: 15,
          evidence_count: 28,
          relationship_count: 20,
          last_observed_at: "2026-03-08T08:05:00Z",
          open_statuses: ["open"],
        }}
      />,
    );

    expect(screen.getByText("Open Findings")).toBeTruthy();
    expect(screen.getByText("6")).toBeTruthy();
    expect(screen.getByText("Critical: 2").className).toContain("border-red");
    expect(screen.getByText("High: 3").className).toContain("border-orange");
    expect(screen.getByText("Assets")).toBeTruthy();
    expect(screen.getByText("10")).toBeTruthy();
    expect(screen.getByText("Vulnerable 4 / Exploited 1")).toBeTruthy();
    expect(screen.getByText("Services")).toBeTruthy();
    expect(screen.getByText("15")).toBeTruthy();
    expect(screen.getByText("Evidence")).toBeTruthy();
    expect(screen.getByText("28")).toBeTruthy();
    expect(screen.getByText("Mar 8, 2026, 8:05 AM UTC")).toBeTruthy();
  });

  it("renders loading summary skeleton state", () => {
    renderWithAuth(<EngagementSummaryCards isLoading />);
    expect(screen.getByLabelText("summary-loading")).toBeTruthy();
    expect(screen.queryByText("No durable engagement summary available yet.")).toBeNull();
  });

  it("renders empty state when summary is unavailable", () => {
    renderWithAuth(<EngagementSummaryCards />);
    expect(screen.getByText("No durable engagement summary available yet.")).toBeTruthy();
  });
});
