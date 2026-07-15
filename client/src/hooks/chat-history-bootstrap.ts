/**
 * Shared transcript-history bootstrap helpers.
 *
 * Contains transport and normalization utilities used by chat bootstrap owners
 * to hydrate transcript state from `/chat/history`.
 */
import { apiFetch } from "@/lib/api-config";
import {
  applyRetryStateUpdate,
  readRetryLifecycleState,
} from "@/state/retry-state-store";
import type { Step } from "@/utils/reasoning-normalizer";

export type ChatHistoryContractVersion = "2026-03-01.chat-history.v2";

export interface ChatTranscriptItem {
  id: string;
  kind: "user" | "assistant" | "reasoning" | "tool" | "observation";
  turn_number: number;
  content: string;
  metadata: Record<string, unknown>;
}

export interface ChatHistoryResponse {
  contractVersion: ChatHistoryContractVersion;
  items: ChatTranscriptItem[];
  nextBeforeTurn: number | null;
  hasMoreOlder: boolean;
  startup?: ChatHistoryStartupPayload | null;
}

export interface ChatHistoryStartupPayload {
  task_id: number;
  conversation_id: string | null;
  checkpointer_ready: boolean;
  tool_catalog_ready: boolean;
  pty_session_ready: boolean;
  runtime_warm: boolean;
  pty_warmup_required: boolean;
  task_running: boolean;
  sse_connected: boolean;
  chat_ready: boolean;
}

export interface ChatInitialHistoryPayload extends ChatHistoryResponse {
  startup?: ChatHistoryStartupPayload | null;
}

export const HISTORY_FETCH_TIMEOUT_MS = 30_000;

function asPositiveNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return Math.floor(value);
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) {
      return Math.floor(parsed);
    }
  }
  return undefined;
}

function asNonNegativeNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
    return Math.floor(value);
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) {
      return Math.floor(parsed);
    }
  }
  return undefined;
}

function resolveReplayTimestamp(value: unknown): string | undefined {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : undefined;
  }
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
    return new Date(value * 1000).toISOString();
  }
  return undefined;
}

function normalizeToolTerminalStatus(value: unknown): string {
  if (typeof value !== "string") {
    return "success";
  }
  const normalized = value.trim().toLowerCase();
  if (!normalized) {
    return "success";
  }
  return normalized;
}

interface FetchChatHistoryPageOptions {
  signal?: AbortSignal;
  conversationId?: string | null;
  beforeTurn?: number;
  limit?: number;
  initial?: boolean;
}

interface FetchInitialChatHistoryOptions {
  signal?: AbortSignal;
  limit?: number;
  conversationId?: string | null;
}

interface FetchOlderTranscriptPageOptions {
  signal?: AbortSignal;
  limit?: number;
  conversationId?: string | null;
  beforeTurn: number;
}

interface FetchLatestTranscriptPageOptions {
  signal?: AbortSignal;
  limit?: number;
  conversationId?: string | null;
}

const initialTranscriptInFlight = new Map<string, Promise<ChatInitialHistoryPayload>>();

function buildInitialHistoryRequestKey(
  taskId: number,
  options?: FetchInitialChatHistoryOptions,
): string {
  const conversationId =
    typeof options?.conversationId === "string" && options.conversationId.trim().length > 0
      ? options.conversationId.trim()
      : "__default__";
  const limit =
    typeof options?.limit === "number" && Number.isFinite(options.limit) && options.limit > 0
      ? Math.floor(options.limit)
      : 0;
  return `${taskId}::${conversationId}::${limit}`;
}

async function fetchChatHistoryPage(
  taskId: number,
  options?: FetchChatHistoryPageOptions,
): Promise<ChatHistoryResponse> {
  const query = new URLSearchParams();
  if (options?.conversationId) {
    query.set("conversation_id", options.conversationId);
  }
  if (typeof options?.beforeTurn === "number" && Number.isFinite(options.beforeTurn) && options.beforeTurn > 0) {
    query.set("before_turn", String(Math.floor(options.beforeTurn)));
  }
  if (typeof options?.limit === "number" && Number.isFinite(options.limit) && options.limit > 0) {
    query.set("limit", String(Math.floor(options.limit)));
  }
  if (options?.initial) {
    query.set("initial", "true");
  }
  const url = query.size ? `/api/tasks/${taskId}/chat/history?${query.toString()}` : `/api/tasks/${taskId}/chat/history`;
  const response = await apiFetch(url, {
    signal: options?.signal,
  });
  if (!response.ok) {
    const text = await response.text();
    const err = new Error(`API Error ${response.status}: ${text}`);
    (err as Error & { status?: number }).status = response.status;
    throw err;
  }
  const data = (await response.json()) as ChatHistoryResponse;
  return {
    contractVersion: data.contractVersion,
    items: Array.isArray(data.items) ? data.items : [],
    nextBeforeTurn: typeof data.nextBeforeTurn === "number" ? data.nextBeforeTurn : null,
    hasMoreOlder: Boolean(data.hasMoreOlder),
    startup: data.startup ?? null,
  };
}

export async function fetchInitialTranscriptPage(
  taskId: number,
  options?: FetchInitialChatHistoryOptions,
): Promise<ChatInitialHistoryPayload> {
  const requestKey = buildInitialHistoryRequestKey(taskId, options);
  const existing = initialTranscriptInFlight.get(requestKey);
  if (existing) {
    return existing;
  }
  const request = (async () => {
    const query = new URLSearchParams();
    query.set("initial", "true");
    if (typeof options?.limit === "number" && Number.isFinite(options.limit) && options.limit > 0) {
      query.set("limit", String(Math.floor(options.limit)));
    }
    if (typeof options?.conversationId === "string" && options.conversationId.trim().length > 0) {
      query.set("conversation_id", options.conversationId.trim());
    }
    const response = await apiFetch(`/api/tasks/${taskId}/chat/history?${query.toString()}`, {
      method: "GET",
      signal: options?.signal,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      const err = new Error(`API Error ${response.status}: ${text}`);
      (err as Error & { status?: number }).status = response.status;
      throw err;
    }
    const payload = (await response.json().catch(() => null)) as ChatInitialHistoryPayload | null;
    if (!payload) {
      throw new Error("Empty initial transcript payload");
    }
    return {
      contractVersion: payload.contractVersion,
      items: Array.isArray(payload.items) ? payload.items : [],
      nextBeforeTurn: typeof payload.nextBeforeTurn === "number" ? payload.nextBeforeTurn : null,
      hasMoreOlder: Boolean(payload.hasMoreOlder),
      startup: payload.startup ?? null,
    };
  })().finally(() => {
    initialTranscriptInFlight.delete(requestKey);
  });
  initialTranscriptInFlight.set(requestKey, request);
  return request;
}

export async function fetchOlderTranscriptPage(
  taskId: number,
  options: FetchOlderTranscriptPageOptions,
): Promise<ChatHistoryResponse> {
  return fetchChatHistoryPage(taskId, {
    signal: options.signal,
    conversationId: options.conversationId,
    limit: options.limit,
    beforeTurn: options.beforeTurn,
  });
}

export async function fetchLatestTranscriptPage(
  taskId: number,
  options?: FetchLatestTranscriptPageOptions,
): Promise<ChatHistoryResponse> {
  return fetchChatHistoryPage(taskId, {
    signal: options?.signal,
    conversationId: options?.conversationId,
    limit: options?.limit,
  });
}

export function normalizeTranscriptItemsToSteps(taskId: number, items: ChatTranscriptItem[]): Step[] {
  const steps: Step[] = [];
  for (const item of items) {
    const sourceMetadata =
      item.metadata && typeof item.metadata === "object" ? item.metadata : {};
    const stepSequence = asPositiveNumber(item.turn_number) ?? 0;
    // Canonical backend phase_sequence is non-negative (0-based), so preserve 0.
    const replaySequence = asNonNegativeNumber(sourceMetadata.sequence) ?? stepSequence;
    const replayTimestamp = resolveReplayTimestamp(sourceMetadata.timestamp);
    const subTurnIndex = asNonNegativeNumber(sourceMetadata.sub_turn_index);
    const metadata: Record<string, unknown> = {
      ...sourceMetadata,
      id: typeof sourceMetadata.id === "string" ? sourceMetadata.id : item.id,
      task_id: taskId,
      transcript_item_id: item.id,
      turn_sequence: stepSequence,
      sequence: replaySequence,
    };
    if (subTurnIndex !== undefined) {
      metadata.sub_turn_index = subTurnIndex;
    }

    if (item.kind === "user" || item.kind === "assistant") {
      const stepType = item.kind === "user" ? "user_message" : "assistant_message";
      steps.push({
        type: stepType,
        content: item.content ?? "",
        metadata: {
          ...metadata,
          step_type: stepType,
          role: item.kind,
          streaming: false,
          is_streaming: false,
          in_progress: false,
        },
        sequence: replaySequence,
        timestamp: replayTimestamp,
        isStreaming: false,
      } as unknown as Step);
      continue;
    }

    if (item.kind === "reasoning" || item.kind === "observation") {
      const prefix = item.kind;
      const ind = asNonNegativeNumber(sourceMetadata.ind) ?? (prefix === "reasoning" ? 0 : 1);
      const sectionName =
        typeof sourceMetadata.section_name === "string" && sourceMetadata.section_name.trim().length > 0
          ? sourceMetadata.section_name
          : prefix;
      const sectionStartedAt = resolveReplayTimestamp(sourceMetadata.started_at);
      const sectionEndedAt = resolveReplayTimestamp(sourceMetadata.ended_at);
      const phaseMetadata = {
        ...metadata,
        ind,
        section_name: sectionName,
      };
      // Emit a full reasoning lifecycle (start -> delta -> section_end) so
      // persisted reasoning sections map 1:1 to Thinking card shells,
      // matching the live streaming contract. Observation items continue
      // to use the simpler delta + section_end pair.
      if (prefix === "reasoning") {
        steps.push({
          type: "reasoning_start",
          content: "",
          metadata: {
            ...phaseMetadata,
            step_type: "reasoning_start",
            step: sectionName,
            streaming: false,
            is_streaming: false,
            in_progress: false,
          },
          sequence: replaySequence,
          timestamp: sectionStartedAt,
          isStreaming: false,
        } as unknown as Step);
      }
      steps.push({
        type: `${prefix}_delta`,
        content: item.content ?? "",
        metadata: {
          ...phaseMetadata,
          step_type: `${prefix}_delta`,
          streaming: false,
          is_streaming: false,
          in_progress: false,
        },
        sequence: replaySequence,
        timestamp: sectionStartedAt ?? replayTimestamp ?? sectionEndedAt,
        isStreaming: false,
      } as unknown as Step);
      steps.push({
        type: `${prefix}_section_end`,
        content: "",
        metadata: {
          ...phaseMetadata,
          step_type: `${prefix}_section_end`,
          streaming: false,
          is_streaming: false,
          in_progress: false,
        },
        sequence: replaySequence,
        timestamp: prefix === "reasoning" ? sectionEndedAt : replayTimestamp,
        isStreaming: false,
      } as unknown as Step);
      continue;
    }

    const ind = asPositiveNumber(sourceMetadata.ind) ?? 1;
    const toolCallId =
      typeof sourceMetadata.tool_call_id === "string" && sourceMetadata.tool_call_id.trim().length > 0
        ? sourceMetadata.tool_call_id.trim()
        : item.id;
    const toolName =
      typeof sourceMetadata.tool_name === "string" && sourceMetadata.tool_name.trim().length > 0
        ? sourceMetadata.tool_name
        : typeof sourceMetadata.tool === "string" && sourceMetadata.tool.trim().length > 0
          ? sourceMetadata.tool
          : typeof sourceMetadata.command === "string" && sourceMetadata.command.trim().length > 0
            ? sourceMetadata.command
            : "unknown";
    const resolvedStatus = normalizeToolTerminalStatus(sourceMetadata.status);
    const toolMetadata = {
      ...metadata,
      ind,
      tool_call_id: toolCallId,
      tool_name: toolName,
      status: resolvedStatus,
    };
    steps.push({
      type: "tool_start",
      content: "",
      metadata: {
        ...toolMetadata,
        step_type: "tool_start",
        streaming: false,
        is_streaming: false,
        in_progress: false,
      },
      sequence: replaySequence,
      timestamp: replayTimestamp,
      isStreaming: false,
    } as unknown as Step);
    steps.push({
      type: "tool_end",
      content: item.content ?? "",
      metadata: {
        ...toolMetadata,
        step_type: "tool_end",
        streaming: false,
        is_streaming: false,
        in_progress: false,
      },
      sequence: replaySequence,
      timestamp: replayTimestamp,
      isStreaming: false,
    } as unknown as Step);
  }
  return steps;
}

export function resetInitialHistoryInFlightForTests(): void {
  initialTranscriptInFlight.clear();
}

function readOptionalNumberMetadata(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return null;
}

function readOptionalStringMetadata(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * Seed the retry-state store from a freshly bootstrapped transcript page.
 *
 * The backend transcript projection is server-authoritative for retry
 * lifecycle. When a workflow row carries an active retry or has settled into a
 * retry-aware terminal state, the matching transcript item metadata exposes
 * ``retry_state`` (and, for in-flight retries, ``active_retry``). This helper
 * trusts that metadata and writes a single canonical entry per
 * ``(task_id, turn_id)`` into the retry-state store so chat surfaces can
 * render the correct CTA disabled state immediately
 * after bootstrap — without re-deriving retryability from local cues.
 *
 * Items without ``retry_state`` and without ``turn_id`` are ignored.
 * Subsequent stream-event updates from ``useMultiTaskStreamManager``
 * remain authoritative and may transition the entry; the store's
 * sticky-terminal invariant ensures a completed retry is not later
 * un-terminated by a stale in-flight event.
 */
export function seedRetryStateFromTranscriptItems(
  taskId: number,
  items: ChatTranscriptItem[],
): void {
  if (!Number.isFinite(taskId) || taskId <= 0 || !Array.isArray(items)) {
    return;
  }
  const seenTurnIds = new Set<string>();
  for (const item of items) {
    const metadata =
      item && typeof item === "object" && item.metadata && typeof item.metadata === "object"
        ? (item.metadata as Record<string, unknown>)
        : null;
    if (!metadata) {
      continue;
    }
    const lifecycleState = readRetryLifecycleState(metadata.retry_state);
    if (!lifecycleState) {
      continue;
    }
    const turnId = readOptionalStringMetadata(metadata.turn_id);
    if (!turnId || seenTurnIds.has(turnId)) {
      continue;
    }
    seenTurnIds.add(turnId);
    applyRetryStateUpdate({
      taskId,
      turnId,
      workflowId: readOptionalNumberMetadata(metadata.workflow_id),
      state: lifecycleState,
      retryAttempt: readOptionalNumberMetadata(metadata.retry_attempt),
      retryMaxAttempts: readOptionalNumberMetadata(metadata.retry_max_attempts),
    });
  }
}
