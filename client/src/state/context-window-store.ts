/**
 * Chat-scoped context-window store keyed by (task_id, conversation_id).
 *
 * This store keeps occupancy snapshots and ordered compaction gates independent
 * from cumulative task usage so the UI can switch task/chat without stale bleed.
 */
import { useSyncExternalStore } from "react";

import {
  normalizeContextCompactionLifecycleEvent,
  reduceContextCompactionGate,
  type ContextCompactionGateState,
} from "@/state/context-compaction-gate";

export type ContextWindowNextAction = "none" | "compress";
export type ContextWindowSnapshotKind = "measured" | "bootstrap_estimate";

export interface ContextWindowSnapshot {
  taskId: number;
  conversationId: string;
  maxTokens: number;
  usedTokens: number;
  remainingTokens: number;
  ratio: number;
  ceilingReached: boolean;
  recommendedNextAction: ContextWindowNextAction;
  compressionCandidate: boolean;
  turnSequence: number | null;
  revision: number;
  snapshotKind: ContextWindowSnapshotKind;
}

interface ContextWindowStoreState {
  snapshot?: ContextWindowSnapshot;
  compactionGate?: ContextCompactionGateState;
}

const store = new Map<string, ContextWindowStoreState>();
const listeners = new Set<() => void>();

const defaultSnapshot: ContextWindowSnapshot = {
  taskId: 0,
  conversationId: "",
  maxTokens: 0,
  usedTokens: 0,
  remainingTokens: 0,
  ratio: 0,
  ceilingReached: false,
  recommendedNextAction: "none",
  compressionCandidate: false,
  turnSequence: null,
  revision: -1,
  snapshotKind: "bootstrap_estimate",
};

function emit(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch (error) {
      console.error("[context-window-store] listener error", error);
    }
  }
}

function keyFor(taskId: number, conversationId: string): string {
  return `${taskId}:${conversationId}`;
}

function normalizeAction(value: unknown): ContextWindowNextAction | null {
  if (value === "none" || value === "compress") return value;
  return null;
}

function normalizeSnapshot(source: Record<string, unknown>): ContextWindowSnapshot | null {
  const taskIdRaw = source.taskId ?? source.task_id;
  const taskId = typeof taskIdRaw === "number" ? taskIdRaw : Number.NaN;
  const conversationIdRaw = source.conversationId ?? source.conversation_id;
  const conversationId = typeof conversationIdRaw === "string" ? conversationIdRaw.trim() : "";
  const maxTokensRaw = source.maxTokens ?? source.max_tokens;
  const usedTokensRaw = source.usedTokens ?? source.used_tokens;
  const remainingTokensRaw = source.remainingTokens ?? source.remaining_tokens;
  const maxTokens = typeof maxTokensRaw === "number" ? maxTokensRaw : Number.NaN;
  const usedTokens = typeof usedTokensRaw === "number" ? usedTokensRaw : Number.NaN;
  const remainingTokens = typeof remainingTokensRaw === "number" ? remainingTokensRaw : Number.NaN;
  const ratioRaw = source.ratio;
  const ratio = typeof ratioRaw === "number" ? ratioRaw : Number.NaN;
  const turnSequenceRaw = source.turnSequence ?? source.turn_sequence;
  const turnSequence =
    turnSequenceRaw === null || turnSequenceRaw === undefined
      ? null
      : typeof turnSequenceRaw === "number"
        ? turnSequenceRaw
        : Number.NaN;
  const revisionRaw = source.revision;
  const revision = typeof revisionRaw === "number" ? revisionRaw : Number.NaN;
  const snapshotKindRaw = source.snapshotKind ?? source.snapshot_kind;

  if (!Number.isInteger(taskId) || taskId <= 0) return null;
  if (!conversationId) return null;
  if (!Number.isInteger(maxTokens) || maxTokens <= 0) return null;
  if (!Number.isInteger(usedTokens) || usedTokens < 0) return null;
  if (!Number.isInteger(remainingTokens) || remainingTokens < 0) return null;
  if (remainingTokens !== Math.max(0, maxTokens - usedTokens)) return null;
  if (!Number.isFinite(ratio) || ratio < 0 || ratio > 1) return null;
  if (Math.abs(ratio - Math.min(1, usedTokens / maxTokens)) > 1e-9) return null;
  if (turnSequence !== null && (!Number.isInteger(turnSequence) || turnSequence < 0)) return null;
  if (!Number.isInteger(revision) || revision < -1) return null;
  if (snapshotKindRaw !== "measured" && snapshotKindRaw !== "bootstrap_estimate") {
    return null;
  }

  const ceilingReached = source.ceilingReached ?? source.ceiling_reached;
  if (typeof ceilingReached !== "boolean") return null;
  const recommendedNextAction = normalizeAction(
    source.recommendedNextAction ?? source.recommended_next_action,
  );
  if (recommendedNextAction === null) return null;
  const compressionCandidate = source.compressionCandidate ?? source.compression_candidate;
  if (typeof compressionCandidate !== "boolean") return null;
  const snapshotKind: ContextWindowSnapshotKind = snapshotKindRaw;
  if (
    snapshotKind === "measured" &&
    (turnSequence === null || revision < 0 || revision !== turnSequence)
  ) {
    return null;
  }
  if (
    snapshotKind === "bootstrap_estimate" &&
    (revision !== -1 || turnSequence !== null)
  ) {
    return null;
  }

  return {
    taskId,
    conversationId,
    maxTokens,
    usedTokens,
    remainingTokens,
    ratio,
    ceilingReached,
    recommendedNextAction,
    compressionCandidate,
    turnSequence,
    revision,
    snapshotKind,
  };
}

function snapshotsEqual(a: ContextWindowSnapshot, b: ContextWindowSnapshot): boolean {
  return (
    a.taskId === b.taskId &&
    a.conversationId === b.conversationId &&
    a.maxTokens === b.maxTokens &&
    a.usedTokens === b.usedTokens &&
    a.remainingTokens === b.remainingTokens &&
    a.ratio === b.ratio &&
    a.ceilingReached === b.ceilingReached &&
    a.recommendedNextAction === b.recommendedNextAction &&
    a.compressionCandidate === b.compressionCandidate &&
    a.turnSequence === b.turnSequence &&
    a.revision === b.revision &&
    a.snapshotKind === b.snapshotKind
  );
}

export function setContextWindowSnapshot(source: Record<string, unknown>): void {
  const nextSnapshot = normalizeSnapshot(source);
  if (!nextSnapshot) return;
  const key = keyFor(nextSnapshot.taskId, nextSnapshot.conversationId);
  const currentState = store.get(key);
  const current = currentState?.snapshot;
  if (current && snapshotsEqual(current, nextSnapshot)) return;
  // Bootstrap estimates share revision -1 and may refresh until a measured turn exists.
  if (
    current?.snapshotKind === "measured" &&
    (nextSnapshot.snapshotKind !== "measured" || nextSnapshot.revision <= current.revision)
  ) {
    return;
  }
  store.set(key, { ...currentState, snapshot: nextSnapshot });
  emit();
}

export function applyContextCompactionLifecycleEvent(
  source: Record<string, unknown>,
  sequenceHint?: unknown,
): void {
  const event = normalizeContextCompactionLifecycleEvent(source, sequenceHint);
  if (!event) return;
  const key = keyFor(event.taskId, event.conversationId);
  const currentState = store.get(key);
  const currentGate = currentState?.compactionGate ?? null;
  const nextGate = reduceContextCompactionGate(currentGate, event);
  if (nextGate === currentGate) return;
  store.set(key, { ...currentState, compactionGate: nextGate });
  emit();
}

export function releaseContextCompactionGatesForTask(taskId: number): void {
  if (!Number.isFinite(taskId) || taskId <= 0) return;
  let changed = false;
  for (const [key, currentState] of store.entries()) {
    const currentGate = currentState.compactionGate;
    if (!currentGate?.active || currentGate.taskId !== taskId) continue;
    store.set(key, {
      ...currentState,
      compactionGate: {
        ...currentGate,
        active: false,
        terminalState: null,
      },
    });
    changed = true;
  }
  if (changed) emit();
}

export function clearContextWindowSnapshot(taskId: number, conversationId: string): void {
  if (!Number.isFinite(taskId) || taskId <= 0) return;
  const normalizedConversationId = conversationId.trim();
  if (!normalizedConversationId) return;
  const key = keyFor(taskId, normalizedConversationId);
  const currentState = store.get(key);
  if (!currentState?.snapshot) return;
  if (currentState.compactionGate) {
    store.set(key, { compactionGate: currentState.compactionGate });
  } else {
    store.delete(key);
  }
  emit();
}

export function getContextWindowSnapshot(
  taskId: number | null,
  conversationId: string | null,
): ContextWindowSnapshot {
  if (!taskId || !conversationId) return defaultSnapshot;
  return store.get(keyFor(taskId, conversationId))?.snapshot ?? defaultSnapshot;
}

export function getContextCompactionGate(
  taskId: number | null,
  conversationId: string | null,
): ContextCompactionGateState | null {
  if (!taskId || !conversationId) return null;
  return store.get(keyFor(taskId, conversationId))?.compactionGate ?? null;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useContextWindowSnapshot(
  taskId: number | null,
  conversationId: string | null,
): ContextWindowSnapshot {
  return useSyncExternalStore(
    subscribe,
    () => getContextWindowSnapshot(taskId, conversationId),
    () => getContextWindowSnapshot(taskId, conversationId),
  );
}

export function useContextCompactionGate(
  taskId: number | null,
  conversationId: string | null,
): ContextCompactionGateState | null {
  return useSyncExternalStore(
    subscribe,
    () => getContextCompactionGate(taskId, conversationId),
    () => getContextCompactionGate(taskId, conversationId),
  );
}

export function resetContextWindowStoreForTests(): void {
  store.clear();
  emit();
}
