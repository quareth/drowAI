/**
 * Usage Insights page shell.
 *
 * Responsibility: own the v1 filter state (`UsageInsightsFilters` + a selected
 * `GroupByKey`) and compose the four focused insights components:
 * `UsageInsightsCards`, `UsageGroupsChart`, `UsageTimelineChart`, and
 * `UsageRecordsTable`. All data fetching is delegated to the hooks in
 * `@/hooks/useUsageInsights` — this shell never talks to the backend directly.
 * Filter UI is intentionally minimal in v1: a group_by selector and an
 * optional conversation_id input (see ownership checklist: modular-ui-tree,
 * single-hook-family, stable-naming, dedicated-usage-page-only).
 */

import { useCallback, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import type { GroupByKey, UsageInsightsFilters } from "@/types/usage";

import { UsageGroupsChart } from "./usage-groups-chart";
import { UsageInsightsCards } from "./usage-insights-cards";
import { UsageRecordsTable } from "./usage-records-table";
import { UsageTimelineChart } from "./usage-timeline-chart";

export interface UsageInsightsPanelProps {
  taskId: number | null | undefined;
}

const GROUP_BY_OPTIONS: ReadonlyArray<{ value: GroupByKey; label: string }> = [
  { value: "role", label: "Role" },
  { value: "node_name", label: "Node" },
  { value: "execution_branch", label: "Branch" },
  { value: "provider", label: "Provider" },
  { value: "model", label: "Model" },
  { value: "api_surface", label: "API surface" },
];

const EMPTY_FILTERS: UsageInsightsFilters = {};

function SectionHeading(props: {
  title: string;
  description?: string;
  id?: string;
}) {
  return (
    <div>
      <h2
        id={props.id}
        className="text-lg font-semibold tracking-tight text-foreground"
      >
        {props.title}
      </h2>
      {props.description ? (
        <p className="text-sm text-muted-foreground">{props.description}</p>
      ) : null}
    </div>
  );
}

export function UsageInsightsPanel({ taskId }: UsageInsightsPanelProps) {
  const [groupBy, setGroupBy] = useState<GroupByKey>("role");
  // Local draft of conversation_id so the user can type without refetching on
  // every keystroke. `filters` is the committed value used by the hooks.
  const [conversationDraft, setConversationDraft] = useState<string>("");
  const [filters, setFilters] = useState<UsageInsightsFilters>(EMPTY_FILTERS);

  const applyConversationFilter = useCallback(() => {
    const trimmed = conversationDraft.trim();
    setFilters((prev) => {
      if (trimmed === "") {
        if (prev.conversation_id == null) return prev;
        const { conversation_id: _omit, ...rest } = prev;
        return rest;
      }
      if (prev.conversation_id === trimmed) return prev;
      return { ...prev, conversation_id: trimmed };
    });
  }, [conversationDraft]);

  const clearFilters = useCallback(() => {
    setConversationDraft("");
    setFilters(EMPTY_FILTERS);
  }, []);

  const hasActiveFilters = useMemo(
    () => Object.values(filters).some((v) => v !== undefined && v !== ""),
    [filters],
  );

  if (taskId == null) {
    return (
      <div className="space-y-4" data-testid="usage-insights-panel">
        <Card>
          <CardHeader>
            <CardTitle className="text-base font-medium">Usage</CardTitle>
            <CardDescription>
              Select a task to see usage insights.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              The usage insights page is task-scoped. Pick a task from the task
              selector to view overview cards, breakdowns, the timeline, and
              per-call records.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div
      className="space-y-6"
      data-testid="usage-insights-panel"
      data-task-id={taskId}
    >
      {/* Controls bar: minimal v1 — group_by + optional conversation_id. */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-medium">Filters</CardTitle>
          <CardDescription>
            Controls apply to every section below. Leave conversation blank to
            include all conversations for this task.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col gap-3 md:flex-row md:items-end md:gap-4">
            <div className="flex flex-col gap-1">
              <Label htmlFor="usage-group-by">Group by</Label>
              <Select
                value={groupBy}
                onValueChange={(value) => setGroupBy(value as GroupByKey)}
              >
                <SelectTrigger
                  id="usage-group-by"
                  className="w-48"
                  data-testid="usage-group-by"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {GROUP_BY_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="flex flex-col gap-1">
              <Label htmlFor="usage-conversation-id">Conversation ID</Label>
              <Input
                id="usage-conversation-id"
                value={conversationDraft}
                onChange={(e) => setConversationDraft(e.target.value)}
                onBlur={applyConversationFilter}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    applyConversationFilter();
                  }
                }}
                placeholder="optional"
                className="w-64"
                data-testid="usage-conversation-id"
              />
            </div>

            <div className="flex-1" />

            <Button
              variant="outline"
              size="sm"
              onClick={clearFilters}
              disabled={!hasActiveFilters && conversationDraft === ""}
              data-testid="usage-clear-filters"
            >
              Clear filters
            </Button>
          </div>
        </CardContent>
      </Card>

      <section className="space-y-3" aria-labelledby="usage-overview-heading">
        <SectionHeading
          id="usage-overview-heading"
          title="Overview"
          description="Server-derived totals, cache metrics, and cost splits."
        />
        <UsageInsightsCards taskId={taskId} filters={filters} />
      </section>

      <section className="space-y-3" aria-labelledby="usage-breakdowns-heading">
        <SectionHeading
          id="usage-breakdowns-heading"
          title="Breakdowns"
          description="Cost grouped by the selected dimension."
        />
        <UsageGroupsChart
          taskId={taskId}
          groupBy={groupBy}
          filters={filters}
        />
      </section>

      <section className="space-y-3" aria-labelledby="usage-timeline-heading">
        <SectionHeading
          id="usage-timeline-heading"
          title="Timeline"
          description="Chronological cumulative cost per LLM call."
        />
        <UsageTimelineChart taskId={taskId} filters={filters} />
      </section>

      <section className="space-y-3" aria-labelledby="usage-records-heading">
        <SectionHeading
          id="usage-records-heading"
          title="Records"
          description="Paginated per-call usage rows with honest cache reporting."
        />
        <UsageRecordsTable taskId={taskId} filters={filters} />
      </section>
    </div>
  );
}

export default UsageInsightsPanel;
