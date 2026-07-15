/**
 * Usage Insights overview cards.
 *
 * Responsibility: render the six top-of-page stat cards for a single task by
 * reading the server-side /usage/insights/overview response verbatim. Handles
 * loading (skeletons), error, and empty/disabled (no task) states uniformly.
 * Never derives or recomputes totals, ratios, or cost splits — it only formats
 * server-provided numbers (see ownership checklist entries:
 * no-frontend-cost-math, server-side-derived-metrics, honest-cache-reporting).
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";

import { useUsageInsightsOverview } from "@/hooks/useUsageInsights";
import {
  formatCostUsd,
  formatPricedCostUsd,
  formatRatio,
  type UsageInsightsFilters,
  type UsageInsightsOverviewResponse,
} from "@/types/usage";

export interface UsageInsightsCardsProps {
  taskId: number | null | undefined;
  filters: UsageInsightsFilters;
}

interface CardViewModel {
  id: string;
  label: string;
  value: string;
  caption?: string;
  /** Optional badge shown next to the value (e.g. "partial coverage"). */
  badge?: { text: string; title?: string };
}

function buildCards(overview: UsageInsightsOverviewResponse): CardViewModel[] {
  const partialCoverage = overview.cache_reporting_coverage < 1;
  const pricingIncomplete = overview.pricing_status !== "available";
  const unpricedProviderLabel = overview.unpriced_providers.join(", ");
  return [
    {
      // Backend-verbatim split: prompt + completion are rendered as two
      // separate numbers rather than summed (no-frontend-cost-math). The
      // backend's overview response intentionally does not carry a combined
      // total_tokens field, so this card shows the pair instead of deriving
      // a sum.
      id: "total_tokens",
      label: "Total tokens",
      value: `${overview.prompt_tokens.toLocaleString()} in`,
      caption: `${overview.completion_tokens.toLocaleString()} out`,
    },
    {
      id: "total_cost",
      label: "Total cost",
      value: formatPricedCostUsd(overview.cost_usd, overview.pricing_status),
      caption: pricingIncomplete
        ? "Provider pricing unavailable for some usage"
        : `cached ${formatCostUsd(overview.cached_input_cost_usd)} · uncached ${formatCostUsd(overview.uncached_input_cost_usd)} · output ${formatCostUsd(overview.output_cost_usd)}`,
      badge: pricingIncomplete
        ? {
            text: overview.pricing_status,
            title: unpricedProviderLabel
              ? `Pricing unavailable for: ${unpricedProviderLabel}`
              : "Pricing is not fully available for these usage rows",
          }
        : undefined,
    },
    {
      id: "cached_tokens",
      label: "Cached tokens",
      value: overview.cached_tokens.toLocaleString(),
      caption: `cache ratio ${formatRatio(overview.cache_ratio)}`,
    },
    {
      id: "cache_hit_rate",
      label: "Cache hit rate",
      value: formatRatio(overview.cache_hit_rate),
      caption: `${overview.cache_hit_calls.toLocaleString()} / ${overview.cache_reporting_call_count.toLocaleString()} reporting calls`,
      badge: partialCoverage
        ? {
            text: "partial coverage",
            title: `Cache reporting coverage: ${formatRatio(overview.cache_reporting_coverage)}`,
          }
        : undefined,
    },
    {
      id: "uncached_prompt_tokens",
      label: "Uncached prompt tokens",
      value: overview.uncached_prompt_tokens.toLocaleString(),
      caption: `prompt ${overview.prompt_tokens.toLocaleString()} · cached ${overview.cached_tokens.toLocaleString()}`,
    },
    {
      id: "call_count",
      label: "Call count",
      value: overview.call_count.toLocaleString(),
      caption: `${overview.cache_reporting_call_count.toLocaleString()} report cache`,
    },
  ];
}

function CardShell(props: { label: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {props.label}
        </CardTitle>
      </CardHeader>
      <CardContent>{props.children}</CardContent>
    </Card>
  );
}

function CardsGrid(props: { children: React.ReactNode }) {
  return (
    <div
      className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
      data-testid="usage-insights-cards"
    >
      {props.children}
    </div>
  );
}

export function UsageInsightsCards({
  taskId,
  filters,
}: UsageInsightsCardsProps) {
  const { data, isLoading, isError, error } = useUsageInsightsOverview(
    taskId,
    filters,
  );

  if (taskId == null) {
    return (
      <CardsGrid>
        <CardShell label="Overview">
          <div className="text-sm text-muted-foreground">
            Select a task to see usage insights.
          </div>
        </CardShell>
      </CardsGrid>
    );
  }

  if (isLoading) {
    const placeholders = [
      "Total tokens",
      "Total cost",
      "Cached tokens",
      "Cache hit rate",
      "Uncached prompt tokens",
      "Call count",
    ];
    return (
      <CardsGrid>
        {placeholders.map((label) => (
          <CardShell key={label} label={label}>
            <Skeleton className="h-7 w-24" />
            <Skeleton className="mt-2 h-4 w-40" />
          </CardShell>
        ))}
      </CardsGrid>
    );
  }

  if (isError || !data) {
    return (
      <CardsGrid>
        <CardShell label="Overview">
          <div className="text-sm text-destructive">
            Failed to load usage insights
            {error instanceof Error ? `: ${error.message}` : ""}.
          </div>
        </CardShell>
      </CardsGrid>
    );
  }

  if (data.call_count === 0) {
    return (
      <CardsGrid>
        <CardShell label="Overview">
          <div className="text-sm text-muted-foreground">
            No LLM calls recorded for this task yet.
          </div>
        </CardShell>
      </CardsGrid>
    );
  }

  const cards = buildCards(data);

  return (
    <CardsGrid>
      {cards.map((card) => (
        <CardShell key={card.id} label={card.label}>
          <div className="flex items-center gap-2">
            <span className="text-2xl font-semibold tabular-nums text-foreground">
              {card.value}
            </span>
            {card.badge ? (
              <Badge
                variant="outline"
                title={card.badge.title}
                data-testid={`usage-card-${card.id}-badge`}
                className="text-xs font-normal"
              >
                {card.badge.text}
              </Badge>
            ) : null}
          </div>
          {card.caption ? (
            <div className="mt-2 text-xs text-muted-foreground">
              {card.caption}
            </div>
          ) : null}
        </CardShell>
      ))}
    </CardsGrid>
  );
}

export default UsageInsightsCards;
