/**
 * Task-scoped stream state store used by chat views and stream consumers.
 *
 * Responsibilities:
 * - hold normalized transcript/render state
 * - track chat readiness + pagination metadata by conversation
 * - coordinate history bootstrap loading claims across mounts
 */
import { useSyncExternalStore } from "react";

import { setChatState } from "@/state/chat-session-store";
import {
  STEP_COMPARATOR,
  mergeStepContent,
  normalizeStep,
  type OpenAIChunk,
  type Step,
} from "@/utils/reasoning-normalizer";
import type { StreamPacket } from "@/types/packets";

interface TaskStreamState {
  items: Step[];
  lastSequence: number;
  streamingKeys: Set<string>;
  isConnected: boolean;
  isConnecting: boolean;
  connectionError: string | null;
  chatReady: boolean;
  chatReadyMeta: Record<string, unknown> | null;
  historyLoaded: boolean;
  historyLoadedByConversation: Set<string>;
  historyLoadingByConversation: Set<string>;
  historyBootstrapTerminalByConversation: Set<string>;
  hasMoreOlderByConversation: Map<string, boolean>;
  nextBeforeCursorByConversation: Map<string, number | null>;
  version: number;
}

interface TaskStreamSnapshot {
  items: Step[];
  lastSequence: number;
  isConnected: boolean;
  isConnecting: boolean;
  connectionError: string | null;
  hasStreaming: boolean;
  chatReady: boolean;
  chatReadyMeta: Record<string, unknown> | null;
  historyLoaded: boolean;
  historyLoadedByConversation: Record<string, true>;
  historyLoadingByConversation: Record<string, true>;
  historyBootstrapTerminalByConversation: Record<string, true>;
  hasMoreOlderByConversation: Record<string, boolean>;
  nextBeforeCursorByConversation: Record<string, number | null>;
  version: number;
}

interface MutationResult {
  changed: boolean;
  itemsChanged?: boolean;
}

const store = new Map<number, TaskStreamState>();
const snapshotCache = new Map<number, TaskStreamSnapshot>();
const listeners = new Set<() => void>();

const defaultSnapshot: TaskStreamSnapshot = {
  items: [],
  lastSequence: 0,
  isConnected: false,
  isConnecting: false,
  connectionError: null,
  hasStreaming: false,
  chatReady: false,
  chatReadyMeta: null,
  historyLoaded: false,
  historyLoadedByConversation: {},
  historyLoadingByConversation: {},
  historyBootstrapTerminalByConversation: {},
  hasMoreOlderByConversation: {},
  nextBeforeCursorByConversation: {},
  version: 0,
};

const DEFAULT_HISTORY_CONVERSATION_KEY = "__default__";

function resolveHistoryConversationKey(conversationId?: string | null): string {
  const normalized = typeof conversationId === "string" ? conversationId.trim() : "";
  return normalized.length > 0 ? normalized : DEFAULT_HISTORY_CONVERSATION_KEY;
}

function buildSnapshot(state: TaskStreamState): TaskStreamSnapshot {
  const historyLoadedByConversation: Record<string, true> = {};
  for (const key of state.historyLoadedByConversation) {
    historyLoadedByConversation[key] = true;
  }
  const historyLoadingByConversation: Record<string, true> = {};
  for (const key of state.historyLoadingByConversation) {
    historyLoadingByConversation[key] = true;
  }
  const historyBootstrapTerminalByConversation: Record<string, true> = {};
  for (const key of state.historyBootstrapTerminalByConversation) {
    historyBootstrapTerminalByConversation[key] = true;
  }
  const hasMoreOlderByConversation: Record<string, boolean> = {};
  for (const [key, hasMoreOlder] of state.hasMoreOlderByConversation.entries()) {
    hasMoreOlderByConversation[key] = hasMoreOlder;
  }
  const nextBeforeCursorByConversation: Record<string, number | null> = {};
  for (const [key, nextBeforeCursor] of state.nextBeforeCursorByConversation.entries()) {
    nextBeforeCursorByConversation[key] = nextBeforeCursor;
  }
  return {
    items: state.items,
    lastSequence: state.lastSequence,
    isConnected: state.isConnected,
    isConnecting: state.isConnecting,
    connectionError: state.connectionError,
    hasStreaming: state.streamingKeys.size > 0,
    chatReady: state.chatReady,
    chatReadyMeta: state.chatReadyMeta,
    historyLoaded: state.historyLoaded,
    historyLoadedByConversation,
    historyLoadingByConversation,
    historyBootstrapTerminalByConversation,
    hasMoreOlderByConversation,
    nextBeforeCursorByConversation,
    version: state.version,
  };
}

function ensureState(taskId: number): TaskStreamState {
  let state = store.get(taskId);
  if (!state) {
    state = {
      items: [],
      lastSequence: 0,
      streamingKeys: new Set<string>(),
      isConnected: false,
      isConnecting: false,
      connectionError: null,
      chatReady: false,
      chatReadyMeta: null,
      historyLoaded: false,
      historyLoadedByConversation: new Set<string>(),
      historyLoadingByConversation: new Set<string>(),
      historyBootstrapTerminalByConversation: new Set<string>(),
      hasMoreOlderByConversation: new Map<string, boolean>(),
      nextBeforeCursorByConversation: new Map<string, number | null>(),
      version: 0,
    };
    store.set(taskId, state);
    snapshotCache.set(taskId, buildSnapshot(state));
  }
  return state;
}

function emit(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch (err) {
      console.error("[chat-stream-store] listener error", err);
    }
  }
}

function mutateTaskState(taskId: number, mutator: (draft: TaskStreamState) => MutationResult): void {
  const current = ensureState(taskId);
  const draft: TaskStreamState = {
    items: [...current.items],
    lastSequence: current.lastSequence,
    streamingKeys: new Set(current.streamingKeys),
    isConnected: current.isConnected,
    isConnecting: current.isConnecting,
    connectionError: current.connectionError,
    chatReady: current.chatReady,
    chatReadyMeta: current.chatReadyMeta,
    historyLoaded: current.historyLoaded,
    historyLoadedByConversation: new Set(current.historyLoadedByConversation),
    historyLoadingByConversation: new Set(current.historyLoadingByConversation),
    historyBootstrapTerminalByConversation: new Set(current.historyBootstrapTerminalByConversation),
    hasMoreOlderByConversation: new Map(current.hasMoreOlderByConversation),
    nextBeforeCursorByConversation: new Map(current.nextBeforeCursorByConversation),
    version: current.version,
  };
  const { changed, itemsChanged } = mutator(draft);
  if (!changed) {
    return;
  }
  if (itemsChanged) {
    draft.items.sort(STEP_COMPARATOR);
  }
  draft.version = current.version + 1;
  store.set(taskId, draft);
  snapshotCache.set(taskId, buildSnapshot(draft));
  emit();
}

function coerceSequence(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return undefined;
  }
  const normalized = Math.floor(value);
  if (normalized < 0) {
    return undefined;
  }
  return normalized;
}

function resolveCursorSequence(step: Step, sequenceHint?: number): number | undefined {
  const metadata = (step.metadata ?? {}) as Record<string, unknown>;
  const candidates = [
    coerceSequence(sequenceHint),
    coerceSequence(metadata.sequence),
    coerceSequence(step.sequence),
  ].filter((value): value is number => typeof value === "number");
  if (candidates.length === 0) {
    return undefined;
  }
  return Math.max(...candidates);
}

function upsertNormalizedStep(draft: TaskStreamState, step: Step | StreamPacket, sequenceHint?: number): MutationResult {
  const normalized = normalizeStep(step);
  const items = draft.items;
  const key = normalized.__internalKey ?? "";
  let changed = false;
  let itemsChanged = false;

  const index = items.findIndex(item => item.__internalKey === key);
  if (index >= 0) {
    const existing = items[index];
    const merged = mergeStepContent(existing, normalized);
    if (merged !== existing) {
      items[index] = merged;
      changed = true;
      itemsChanged = true;
    }
  } else {
    items.push(normalized);
    changed = true;
    itemsChanged = true;
  }

  const cursorSequence = resolveCursorSequence(normalized, sequenceHint);
  if (typeof cursorSequence === "number" && cursorSequence > draft.lastSequence) {
    draft.lastSequence = cursorSequence;
    changed = true;
  }

  if (normalized.isStreaming && key) {
    if (!draft.streamingKeys.has(key)) {
      draft.streamingKeys.add(key);
      changed = true;
    }
  } else if (key && draft.streamingKeys.has(key)) {
    draft.streamingKeys.delete(key);
    changed = true;
  }

  return { changed, itemsChanged };
}

function clearStreamingItems(draft: TaskStreamState, sequence?: number): MutationResult {
  let changed = false;
  let itemsChanged = false;
  const cleared: string[] = [];
  draft.items = draft.items.map(item => {
    if (!item.isStreaming) return item;
    if (typeof sequence === "number") {
      const metaSeq = (item.metadata as Record<string, unknown> | undefined)?.turn_sequence;
      const itemSequence =
        typeof metaSeq === "number"
          ? metaSeq
          : typeof item.sequence === "number"
            ? item.sequence
            : undefined;
      if (itemSequence !== sequence) {
        return item;
      }
    }
    const metadata = {
      ...(item.metadata ?? {}),
      streaming: false,
      is_streaming: false,
      in_progress: false,
    } as Record<string, unknown>;
    const updated: Step = {
      ...item,
      metadata,
      isStreaming: false,
    };
    if (item.__internalKey) {
      cleared.push(item.__internalKey);
    }
    changed = true;
    itemsChanged = true;
    return updated;
  });
  for (const key of cleared) {
    draft.streamingKeys.delete(key);
  }
  return { changed, itemsChanged };
}

function isAssistantFinal(message: Step | StreamPacket): boolean {
  const normalized = normalizeStep(message);
  const metadata = (normalized.metadata ?? {}) as Record<string, unknown>;
  return normalized.type === "assistant_final" || metadata.subtype === "assistant_final";
}

export function setTaskHistory(
  taskId: number,
  steps: Step[],
  options?: { markHistoryLoaded?: boolean; conversationId?: string | null },
): void {
  if (!taskId) return;
  if (steps.length > 0) {
    mutateTaskState(taskId, draft => {
      let changed = false;
      let itemsChanged = false;
      for (const raw of steps) {
        const result = upsertNormalizedStep(draft, raw);
        if (result.changed) {
          changed = true;
        }
        if (result.itemsChanged) {
          itemsChanged = true;
        }
      }
      return { changed, itemsChanged };
    });
  }
  if (options?.markHistoryLoaded !== false) {
    setHistoryLoaded(taskId, options?.conversationId);
  }
}

export function applyStreamMessage(taskId: number, message: Step | StreamPacket, sequenceHint?: number): void {
  if (!taskId) return;
  const terminal = isAssistantFinal(message);
  mutateTaskState(taskId, draft => {
    const completion = terminal
      ? clearStreamingItems(draft)
      : { changed: false, itemsChanged: false };
    const upsert = upsertNormalizedStep(draft, message, sequenceHint);
    return {
      changed: completion.changed || upsert.changed,
      itemsChanged: completion.itemsChanged || upsert.itemsChanged,
    };
  });
  if (terminal) {
    setChatState(taskId, "input");
  }
}

export function appendStreamingChunk(taskId: number, chunk: OpenAIChunk, fallbackSequence?: number): void {
  if (!taskId) return;
  const textDelta = chunk?.choices?.[0]?.delta?.content ?? "";
  if (!textDelta) {
    return;
  }
  const chunkMetadata = chunk.metadata ?? {};
  mutateTaskState(taskId, draft => {
    const sequence = typeof chunk.sequence === "number" ? chunk.sequence : fallbackSequence;
    const step: Step = {
      sequence,
      type: "assistant_delta",
      content: textDelta,
      metadata: {
        id: chunk.id ?? undefined,
        streaming: true,
        is_streaming: true,
        ind: chunkMetadata.ind,
        step_type: chunkMetadata.step_type,
        conversation_id: chunkMetadata.conversation_id ?? chunkMetadata.conversationId,
      },
      isStreaming: true,
    };
    return upsertNormalizedStep(draft, step, sequence);
  });
}

export function advanceStreamSequence(taskId: number, sequence?: number): void {
  if (!taskId) return;
  const nextSequence = coerceSequence(sequence);
  if (nextSequence === undefined) {
    return;
  }
  mutateTaskState(taskId, draft => {
    if (nextSequence <= draft.lastSequence) {
      return { changed: false };
    }
    draft.lastSequence = nextSequence;
    return { changed: true, itemsChanged: false };
  });
}

export function markStreamingComplete(taskId: number, sequence?: number): void {
  if (!taskId) return;
  mutateTaskState(taskId, draft => clearStreamingItems(draft, sequence));
  setChatState(taskId, "input");
}

export function setConnectionState(
  taskId: number,
  update: Partial<{ isConnected: boolean; isConnecting: boolean; connectionError: string | null }>,
): void {
  if (!taskId) return;
  mutateTaskState(taskId, draft => {
    let changed = false;
    if (typeof update.isConnected === "boolean" && draft.isConnected !== update.isConnected) {
      draft.isConnected = update.isConnected;
      changed = true;
    }
    if (typeof update.isConnecting === "boolean" && draft.isConnecting !== update.isConnecting) {
      draft.isConnecting = update.isConnecting;
      changed = true;
    }
    if ("connectionError" in update && draft.connectionError !== (update.connectionError ?? null)) {
      draft.connectionError = update.connectionError ?? null;
      changed = true;
    }
    return { changed, itemsChanged: false };
  });
}

export function setChatReadyState(taskId: number, chatReady: boolean, meta?: Record<string, unknown> | null): void {
  if (!taskId) return;
  mutateTaskState(taskId, draft => {
    let changed = false;
    if (draft.chatReady !== chatReady) {
      draft.chatReady = chatReady;
      changed = true;
    }
    if (meta !== undefined && draft.chatReadyMeta !== meta) {
      draft.chatReadyMeta = meta;
      changed = true;
    }
    return { changed, itemsChanged: false };
  });
}

export function setHistoryLoaded(taskId: number, conversationId?: string | null): void {
  if (!taskId) return;
  const historyKey = resolveHistoryConversationKey(conversationId);
  mutateTaskState(taskId, draft => {
    const loadingCleared = draft.historyLoadingByConversation.delete(historyKey);
    const terminalCleared = draft.historyBootstrapTerminalByConversation.delete(historyKey);
    if (draft.historyLoadedByConversation.has(historyKey)) {
      return { changed: loadingCleared || terminalCleared, itemsChanged: false };
    }
    draft.historyLoadedByConversation.add(historyKey);
    draft.historyLoaded = draft.historyLoadedByConversation.size > 0;
    return { changed: true, itemsChanged: false };
  });
}

/**
 * Marks bootstrap as terminal for this task/conversation.
 * Used for non-recoverable bootstrap errors (for example 404 after task deletion).
 */
export function markHistoryBootstrapTerminal(taskId: number, conversationId?: string | null): void {
  if (!taskId) return;
  const historyKey = resolveHistoryConversationKey(conversationId);
  mutateTaskState(taskId, draft => {
    const loadingCleared = draft.historyLoadingByConversation.delete(historyKey);
    if (draft.historyBootstrapTerminalByConversation.has(historyKey)) {
      return { changed: loadingCleared, itemsChanged: false };
    }
    draft.historyBootstrapTerminalByConversation.add(historyKey);
    return { changed: true, itemsChanged: false };
  });
}

export function clearHistoryBootstrapTerminal(taskId: number, conversationId?: string | null): void {
  if (!taskId) return;
  const historyKey = resolveHistoryConversationKey(conversationId);
  mutateTaskState(taskId, draft => {
    if (!draft.historyBootstrapTerminalByConversation.has(historyKey)) {
      return { changed: false };
    }
    draft.historyBootstrapTerminalByConversation.delete(historyKey);
    return { changed: true, itemsChanged: false };
  });
}

export function setHistoryLoading(
  taskId: number,
  isLoading: boolean,
  conversationId?: string | null,
): void {
  if (!taskId) return;
  const historyKey = resolveHistoryConversationKey(conversationId);
  mutateTaskState(taskId, draft => {
    const alreadyLoading = draft.historyLoadingByConversation.has(historyKey);
    if (isLoading) {
      if (alreadyLoading) {
        return { changed: false };
      }
      draft.historyLoadingByConversation.add(historyKey);
      return { changed: true, itemsChanged: false };
    }
    if (!alreadyLoading) {
      return { changed: false };
    }
    draft.historyLoadingByConversation.delete(historyKey);
    return { changed: true, itemsChanged: false };
  });
}

export function tryStartHistoryLoading(taskId: number, conversationId?: string | null): boolean {
  if (!taskId) return false;
  const historyKey = resolveHistoryConversationKey(conversationId);
  let acquired = false;
  mutateTaskState(taskId, draft => {
    if (draft.historyLoadingByConversation.has(historyKey)) {
      return { changed: false };
    }
    draft.historyLoadingByConversation.add(historyKey);
    acquired = true;
    return { changed: true, itemsChanged: false };
  });
  return acquired;
}

function coerceBeforeCursor(value: number | null | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return null;
  }
  return Math.floor(value);
}

function resolveMonotonicBeforeCursor(
  previousCursor: number | null,
  incomingCursor: number | null,
  hasMoreOlder: boolean,
): number | null {
  if (!hasMoreOlder) {
    return null;
  }
  if (incomingCursor === null) {
    return previousCursor;
  }
  if (previousCursor === null) {
    return incomingCursor;
  }
  return incomingCursor < previousCursor ? incomingCursor : previousCursor;
}

export function setTranscriptPaginationState(
  taskId: number,
  options: {
    conversationId?: string | null;
    hasMoreOlder: boolean;
    nextBeforeCursor: number | null | undefined;
  },
): void {
  if (!taskId) return;
  const historyKey = resolveHistoryConversationKey(options.conversationId);
  const nextBeforeCursor = coerceBeforeCursor(options.nextBeforeCursor);
  mutateTaskState(taskId, draft => {
    const previousHasMoreOlder = draft.hasMoreOlderByConversation.get(historyKey) ?? false;
    const previousNextBeforeCursor = draft.nextBeforeCursorByConversation.get(historyKey) ?? null;
    const resolvedNextBeforeCursor = resolveMonotonicBeforeCursor(
      previousNextBeforeCursor,
      nextBeforeCursor,
      options.hasMoreOlder,
    );
    const resolvedHasMoreOlder = options.hasMoreOlder && resolvedNextBeforeCursor !== null;
    let changed = false;

    if (previousHasMoreOlder !== resolvedHasMoreOlder) {
      draft.hasMoreOlderByConversation.set(historyKey, resolvedHasMoreOlder);
      changed = true;
    }

    if (previousNextBeforeCursor !== resolvedNextBeforeCursor) {
      draft.nextBeforeCursorByConversation.set(historyKey, resolvedNextBeforeCursor);
      changed = true;
    }

    return { changed, itemsChanged: false };
  });
}

export function hasConversationHistoryLoaded(
  taskId: number | null,
  conversationId?: string | null,
): boolean {
  if (!taskId) return false;
  const state = store.get(taskId);
  if (!state) return false;
  return state.historyLoadedByConversation.has(resolveHistoryConversationKey(conversationId));
}

export function isConversationHistoryLoading(
  taskId: number | null,
  conversationId?: string | null,
): boolean {
  if (!taskId) return false;
  const state = store.get(taskId);
  if (!state) return false;
  return state.historyLoadingByConversation.has(resolveHistoryConversationKey(conversationId));
}

export function isHistoryBootstrapTerminal(
  taskId: number | null,
  conversationId?: string | null,
): boolean {
  if (!taskId) return false;
  const state = store.get(taskId);
  if (!state) return false;
  return state.historyBootstrapTerminalByConversation.has(resolveHistoryConversationKey(conversationId));
}

export function getConversationHasMoreOlder(
  taskId: number | null,
  conversationId?: string | null,
): boolean {
  if (!taskId) return false;
  const state = store.get(taskId);
  if (!state) return false;
  return state.hasMoreOlderByConversation.get(resolveHistoryConversationKey(conversationId)) ?? false;
}

export function getConversationNextBeforeCursor(
  taskId: number | null,
  conversationId?: string | null,
): number | null {
  if (!taskId) return null;
  const state = store.get(taskId);
  if (!state) return null;
  return state.nextBeforeCursorByConversation.get(resolveHistoryConversationKey(conversationId)) ?? null;
}

/**
 * Clears render state so a forced resync can rehydrate from canonical history.
 * Keeps cursor monotonic to avoid reconnecting behind known sequence.
 */
export function resetTaskStreamForResync(taskId: number, sequence?: number): void {
  if (!taskId) return;
  mutateTaskState(taskId, draft => {
    const seq = coerceSequence(sequence);
    const nextLastSequence =
      typeof seq === "number" ? Math.max(draft.lastSequence, seq) : draft.lastSequence;
    const shouldClear =
      draft.items.length > 0 ||
      draft.streamingKeys.size > 0 ||
      draft.historyLoaded ||
      draft.historyLoadingByConversation.size > 0 ||
      draft.historyBootstrapTerminalByConversation.size > 0 ||
      draft.hasMoreOlderByConversation.size > 0 ||
      draft.nextBeforeCursorByConversation.size > 0 ||
      nextLastSequence !== draft.lastSequence;
    if (!shouldClear) {
      return { changed: false };
    }
    draft.items = [];
    draft.streamingKeys.clear();
    draft.historyLoadedByConversation.clear();
    draft.historyLoadingByConversation.clear();
    draft.historyBootstrapTerminalByConversation.clear();
    draft.hasMoreOlderByConversation.clear();
    draft.nextBeforeCursorByConversation.clear();
    draft.historyLoaded = false;
    draft.lastSequence = nextLastSequence;
    return { changed: true, itemsChanged: false };
  });
}

/** Clears all state for the task. Deletion inherently resets all flags (historyLoaded, chatReady, etc.). */
export function clearTaskState(taskId: number): void {
  if (!taskId) return;
  if (!store.has(taskId)) return;
  store.delete(taskId);
  snapshotCache.delete(taskId);
  emit();
}

function getSnapshot(taskId: number | null): TaskStreamSnapshot {
  if (!taskId) {
    return defaultSnapshot;
  }
  const cached = snapshotCache.get(taskId);
  if (cached) {
    return cached;
  }
  const state = store.get(taskId);
  if (!state) {
    return defaultSnapshot;
  }
  const snapshot = buildSnapshot(state);
  snapshotCache.set(taskId, snapshot);
  return snapshot;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useTaskStreamSnapshot(taskId: number | null) {
  return useSyncExternalStore(subscribe, () => getSnapshot(taskId), () => getSnapshot(taskId));
}

/** Read-only snapshot helper for non-React callers (tests/tooling). */
export function getTaskStreamSnapshot(taskId: number | null): TaskStreamSnapshot {
  return getSnapshot(taskId);
}

export function getLastSequence(taskId: number | null): number {
  if (!taskId) return 0;
  const state = store.get(taskId);
  return state?.lastSequence ?? 0;
}

export function hasStreaming(taskId: number | null): boolean {
  if (!taskId) return false;
  const state = store.get(taskId);
  return state ? state.streamingKeys.size > 0 : false;
}
