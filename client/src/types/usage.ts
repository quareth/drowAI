/**
 * Token usage types for DrowAI frontend.
 * 
 * These types mirror the backend response models from
 * backend/routers/usage.py endpoints.
 */

/**
 * Aggregated token usage for a task.
 * Response from GET /api/tasks/{task_id}/usage
 */
export interface TokenUsage {
  task_id: number;
  /** Total input/prompt tokens across all LLM calls */
  prompt_tokens: number;
  /** Total output/completion tokens across all LLM calls */
  completion_tokens: number;
  /** Sum of prompt + completion tokens */
  total_tokens: number;
  /** Tokens served from cache (discounted rate) */
  cached_tokens: number;
  /** Extended thinking tokens (GPT-5 models) */
  reasoning_tokens: number;
  /** Total cost in USD */
  cost_usd: number;
  /** Whether every provider/model row could be priced server-side. */
  pricing_status: PricingStatus;
  /** Providers whose token usage is present but pricing is unavailable. */
  unpriced_providers: string[];
  /** Provider/model refs whose token usage is present but pricing is unavailable. */
  unpriced_models: string[];
  /** Number of LLM API calls made */
  call_count: number;
  /** List of models used in this task */
  models: string[];
  /** ISO timestamp of first LLM call */
  first_call: string | null;
  /** ISO timestamp of last LLM call */
  last_call: string | null;
}

/**
 * Individual LLM call usage record.
 * Part of the breakdown response.
 */
export interface UsageBreakdownItem {
  id: number;
  /** LLM provider used for this call (e.g., "openai", "anthropic") */
  provider: string;
  /** Model used for this call (e.g., "gpt-4o-mini") */
  model: string;
  /** Source identifier (e.g., "langgraph", "chat_router") */
  source: string;
  /** Input tokens for this call */
  prompt_tokens: number;
  /** Output tokens for this call */
  completion_tokens: number;
  /** Total tokens for this call */
  total_tokens: number;
  /** Cached tokens for this call */
  cached_tokens: number;
  /** Reasoning tokens for this call (GPT-5) */
  reasoning_tokens: number;
  /** Cost in USD for this call */
  cost_usd: number;
  /** Whether this row could be priced server-side. */
  pricing_status: PricingStatus;
  /** ISO timestamp when this call was made */
  created_at: string;
  /** Optional conversation ID */
  conversation_id: string | null;
}

/**
 * Paginated breakdown of per-call usage.
 * Response from GET /api/tasks/{task_id}/usage/breakdown
 */
export interface UsageBreakdownResponse {
  task_id: number;
  items: UsageBreakdownItem[];
  total_count: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

/**
 * Legacy usage cost response.
 * Response from GET /api/tasks/{task_id}/usage/cost (deprecated)
 * 
 * @deprecated Use TokenUsage from /api/tasks/{task_id}/usage instead
 */
export interface LegacyUsageCostResponse {
  taskId: number;
  provider?: string;
  model: string;
  output_tokens: number;
  input_tokens?: number;
  total_tokens: number;
  price_per_1k: number;
  cost_usd: number;
  pricing_status?: PricingStatus;
  unpriced_models?: string[];
  // New fields added for clients that support them
  prompt_tokens?: number;
  completion_tokens?: number;
  cached_tokens?: number;
  call_count?: number;
}

/**
 * Format cost as a display string.
 * Handles small values with more precision.
 */
export function formatCostUSD(cost: number): string {
  if (cost === 0) return "$0.00";
  if (cost < 0.0001) return `$${cost.toFixed(6)}`;
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

/** Format cost with explicit unavailable-pricing handling. */
export function formatPricedCostUSD(
  cost: number,
  pricingStatus: PricingStatus | undefined,
): string {
  if (pricingStatus === "unavailable") return "Unavailable";
  if (pricingStatus === "partial") return `${formatCostUSD(cost)} partial`;
  if (pricingStatus === "estimated") return `${formatCostUSD(cost)} estimated`;
  return formatCostUSD(cost);
}

/**
 * Format token count with thousands separator.
 */
export function formatTokenCount(tokens: number): string {
  return tokens.toLocaleString();
}

/**
 * Get a human-readable summary of usage.
 */
export function getUsageSummary(usage: TokenUsage): string {
  const parts: string[] = [];
  
  if (usage.prompt_tokens > 0) {
    parts.push(`${formatTokenCount(usage.prompt_tokens)} in`);
  }
  if (usage.completion_tokens > 0) {
    parts.push(`${formatTokenCount(usage.completion_tokens)} out`);
  }
  if (usage.cached_tokens > 0) {
    parts.push(`${formatTokenCount(usage.cached_tokens)} cached`);
  }
  
  return parts.join(" • ") || "No usage";
}

// ---------------------------------------------------------------------------
// Usage Insights (Phase 3)
//
// Typed mirrors of the Pydantic v2 response models in
// backend/schemas/usage_insights.py. These types carry server-derived cache
// and cost metrics verbatim; no client-side recomputation is allowed
// (see ownership checklist: server-side-derived-metrics, no-frontend-cost-math).
// ---------------------------------------------------------------------------

/**
 * Canonical grouping-key set accepted by
 * GET /api/tasks/{task_id}/usage/insights/groups.
 *
 * Mirrors `GroupByKey` in backend/schemas/usage_insights.py. "source" is
 * deliberately absent — role/branch/provider/etc. come from the canonical
 * `request_metadata` contract, never parsed from the legacy `source` string.
 */
export type GroupByKey =
  | "role"
  | "node_name"
  | "execution_branch"
  | "provider"
  | "model"
  | "api_surface";

/**
 * The three honest cache-reporting states a per-call record can carry.
 *
 * Mirrors `CacheReporting` in backend/schemas/usage_insights.py. Non-reporting
 * provider surfaces are labeled `"not_reported"`, never silently rendered as
 * a definitive `0` cache (see ownership checklist: honest-cache-reporting).
 */
export type CacheReporting = "reported" | "not_reported" | "unknown";

/** Whether usage cost values are complete for a response or row. */
export type PricingStatus = "available" | "partial" | "unavailable" | "estimated";

/**
 * Optional filter envelope accepted by every insights endpoint.
 *
 * Each field is optional; an absent/undefined value means "no filter on this
 * dimension". To target historical rows with missing canonical metadata,
 * pass the literal string `"unknown"` for the relevant field (matches the
 * backend's explicit-unknown-buckets contract).
 */
export interface UsageInsightsFilters {
  conversation_id?: string;
  provider?: string;
  model?: string;
  role?: string;
  execution_branch?: string;
}

/**
 * Per-task rollup with server-derived cache and cost metrics.
 * Response from GET /api/tasks/{task_id}/usage/insights/overview.
 *
 * Mirrors `UsageInsightsOverviewResponse` in
 * backend/schemas/usage_insights.py. Every derived numeric field is computed
 * server-side; frontend renders them verbatim.
 */
export interface UsageInsightsOverviewResponse {
  task_id: number;
  /** Per-provider call counts, e.g. {"openai": 42, "unknown": 3}. */
  provider_coverage: Record<string, number>;
  call_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  /** max(0, prompt_tokens - cached_tokens), backend-computed. */
  uncached_prompt_tokens: number;
  /** Calls with cached_tokens > 0 AND cache_reporting == "reported". */
  cache_hit_calls: number;
  /** cache_hit_calls / cache_reporting_call_count (0 when denominator is 0). */
  cache_hit_rate: number;
  /** Token-weighted ratio restricted to reporting rows (0 when unavailable). */
  cache_ratio: number;
  cache_reporting_call_count: number;
  /** cache_reporting_call_count / call_count (0 when denominator is 0). */
  cache_reporting_coverage: number;
  cost_usd: number;
  cached_input_cost_usd: number;
  uncached_input_cost_usd: number;
  output_cost_usd: number;
  pricing_status: PricingStatus;
  unpriced_providers: string[];
  unpriced_models: string[];
}

/**
 * One aggregated bucket row for the groups endpoint.
 *
 * Mirrors `UsageInsightsGroupRow` in backend/schemas/usage_insights.py.
 * `bucket_key` preserves the normalized metadata value verbatim, including
 * the literal string `"unknown"` for rows whose metadata was missing.
 */
export interface UsageInsightsGroupRow {
  bucket_key: string;
  call_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  uncached_prompt_tokens: number;
  cache_hit_calls: number;
  cache_hit_rate: number;
  cache_ratio: number;
  cache_reporting_call_count: number;
  cache_reporting_coverage: number;
  cost_usd: number;
  cached_input_cost_usd: number;
  uncached_input_cost_usd: number;
  output_cost_usd: number;
  pricing_status: PricingStatus;
}

/**
 * Grouped breakdown response for a single `group_by` dimension.
 * Response from GET /api/tasks/{task_id}/usage/insights/groups.
 *
 * Mirrors `UsageInsightsGroupsResponse` in backend/schemas/usage_insights.py.
 */
export interface UsageInsightsGroupsResponse {
  task_id: number;
  group_by: GroupByKey;
  items: UsageInsightsGroupRow[];
}

/**
 * One chronological call-level timeline point.
 *
 * Mirrors `UsageInsightsTimelinePoint` in backend/schemas/usage_insights.py.
 * `created_at` arrives as an ISO-8601 string (matches /usage/breakdown).
 * `cumulative_*` fields are server-computed running sums in chronological
 * order; no cumulative ratio is exposed intentionally.
 */
export interface UsageInsightsTimelinePoint {
  /** ISO-8601 timestamp (e.g. "2026-04-14T12:34:56.789000"). */
  created_at: string;
  provider: string;
  role: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cost_usd: number;
  pricing_status: PricingStatus;
  /** Per-row ratio; 0 when prompt_tokens==0 or cache_reporting != "reported". */
  cache_ratio: number;
  cumulative_prompt_tokens: number;
  cumulative_completion_tokens: number;
  cumulative_cached_tokens: number;
  cumulative_cost_usd: number;
}

/**
 * Chronological per-call timeline for one task.
 * Response from GET /api/tasks/{task_id}/usage/insights/timeline.
 *
 * Mirrors `UsageInsightsTimelineResponse` in
 * backend/schemas/usage_insights.py.
 */
export interface UsageInsightsTimelineResponse {
  task_id: number;
  items: UsageInsightsTimelinePoint[];
}

/**
 * One detail row with the full canonical metadata contract.
 *
 * Mirrors `UsageInsightsRecord` in backend/schemas/usage_insights.py.
 * Canonical metadata fields (`role`, `node_name`, `execution_branch`,
 * `provider`, `api_surface`, `request_mode`) default to the literal string
 * `"unknown"` for rows with missing metadata. `cache_reporting` is the
 * closed `CacheReporting` Literal.
 *
 * `source` is surfaced for debug visibility only — the UI MUST NOT parse it
 * for role/branch (see ownership checklist: no-source-as-grouping-key).
 */
export interface UsageInsightsRecord {
  id: number;
  /** ISO-8601 timestamp; "" for a null upstream value (legacy convention). */
  created_at: string;
  model: string;
  source: string;
  conversation_id: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
  pricing_status: PricingStatus;
  role: string;
  node_name: string;
  execution_branch: string;
  provider: string;
  api_surface: string;
  request_mode: string;
  cache_reporting: CacheReporting;
  turn_index: number | null;
}

/**
 * Paginated detail-record response.
 * Response from GET /api/tasks/{task_id}/usage/insights/records.
 *
 * Mirrors `UsageInsightsRecordsResponse` in
 * backend/schemas/usage_insights.py.
 */
export interface UsageInsightsRecordsResponse {
  task_id: number;
  items: UsageInsightsRecord[];
  total_count: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

// ---------------------------------------------------------------------------
// Shared formatters for ratios, percentages, and cache-status labels.
//
// Pure, presentation-only. MUST NOT derive any new metric — they only format
// server-computed numbers (see ownership checklist: no-frontend-cost-math).
// ---------------------------------------------------------------------------

/**
 * Format a ratio in [0, 1] as a one-decimal percentage string.
 *
 * Convention: `formatRatio(0)` returns `"0.0%"` (one decimal, matching
 * non-zero renders). Values outside [0, 1] are clamped so rendering never
 * surfaces negative or >100% percentages from a contract bug; non-finite
 * inputs fall back to `"0.0%"`.
 *
 * @example formatRatio(0.1234) // "12.3%"
 * @example formatRatio(0)      // "0.0%"
 * @example formatRatio(1)      // "100.0%"
 */
export function formatRatio(value: number): string {
  if (!Number.isFinite(value)) return "0.0%";
  const clamped = value < 0 ? 0 : value > 1 ? 1 : value;
  return `${(clamped * 100).toFixed(1)}%`;
}

/**
 * Format a USD cost value for display.
 *
 * - `0` renders as `"$0.00"`.
 * - Positive values `< $0.01` use 4 decimals (e.g. `"$0.0042"`) so tiny
 *   per-call costs stay visible.
 * - Everything else uses 2 decimals (e.g. `"$1.82"`).
 * - Non-finite inputs fall back to `"$0.00"`.
 *
 * Formats a single backend-provided number; never sums or derives a new
 * cost value.
 */
export function formatCostUsd(value: number): string {
  if (!Number.isFinite(value)) return "$0.00";
  if (value === 0) return "$0.00";
  const abs = Math.abs(value);
  if (abs < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

/** Format backend-provided cost while preserving unavailable pricing status. */
export function formatPricedCostUsd(
  value: number,
  pricingStatus: PricingStatus | undefined,
): string {
  if (pricingStatus === "unavailable") return "Unavailable";
  if (pricingStatus === "partial") return `${formatCostUsd(value)} partial`;
  if (pricingStatus === "estimated") return `${formatCostUsd(value)} estimated`;
  return formatCostUsd(value);
}

/**
 * Map a {@link CacheReporting} value to a human-readable label.
 *
 * @example formatCacheReportingLabel("reported")     // "Reported"
 * @example formatCacheReportingLabel("not_reported") // "Not reported"
 * @example formatCacheReportingLabel("unknown")      // "Unknown"
 */
export function formatCacheReportingLabel(value: CacheReporting): string {
  switch (value) {
    case "reported":
      return "Reported";
    case "not_reported":
      return "Not reported";
    case "unknown":
      return "Unknown";
  }
}
