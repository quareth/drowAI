import { useSyncExternalStore } from "react";

export type ChatState = "input" | "loading" | "streaming";

interface ChatSessionState {
  conversationId: string | null;
  version: number;
  chatState: ChatState;
}

interface ChatSessionSnapshot {
  conversationId: string | null;
  version: number;
  chatState: ChatState;
}

const store = new Map<number, ChatSessionState>();
const snapshotCache = new Map<number, ChatSessionSnapshot>();
const listeners = new Set<() => void>();

const defaultSnapshot: ChatSessionSnapshot = {
  conversationId: null,
  version: 0,
  chatState: "input",
};

function buildSnapshot(state: ChatSessionState): ChatSessionSnapshot {
  return {
    conversationId: state.conversationId,
    version: state.version,
    chatState: state.chatState,
  };
}

function ensureState(taskId: number): ChatSessionState {
  let state = store.get(taskId);
  if (!state) {
    state = { conversationId: null, version: 0, chatState: "input" };
    store.set(taskId, state);
    snapshotCache.set(taskId, buildSnapshot(state));
  }
  return state;
}

function emit(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch (error) {
      console.error("[chat-session-store] listener error", error);
    }
  }
}

function setState(taskId: number, updater: (draft: ChatSessionState) => boolean): void {
  const current = ensureState(taskId);
  const draft: ChatSessionState = {
    conversationId: current.conversationId,
    version: current.version,
    chatState: current.chatState,
  };
  const changed = updater(draft);
  if (!changed) return;
  draft.version = current.version + 1;
  store.set(taskId, draft);
  snapshotCache.set(taskId, buildSnapshot(draft));
  emit();
}

export function setConversationId(taskId: number, conversationId: string | null): void {
  if (!taskId) return;
  const normalizedConversationId =
    typeof conversationId === "string" ? conversationId.trim() : "";
  setState(taskId, draft => {
    const nextConversationId = normalizedConversationId.length > 0 ? normalizedConversationId : null;
    if (draft.conversationId === nextConversationId) {
      return false;
    }
    draft.conversationId = nextConversationId;
    return true;
  });
}

export function setChatState(taskId: number, chatState: ChatState): void {
  if (!taskId) return;
  setState(taskId, draft => {
    if (draft.chatState === chatState) {
      return false;
    }
    draft.chatState = chatState;
    return true;
  });
}

export function clearChatSession(taskId: number): void {
  if (!store.has(taskId)) return;
  store.delete(taskId);
  snapshotCache.delete(taskId);
  emit();
}

function getSnapshot(taskId: number | null): ChatSessionSnapshot {
  if (!taskId) return defaultSnapshot;
  const cached = snapshotCache.get(taskId);
  if (cached) return cached;
  const state = store.get(taskId);
  if (!state) return defaultSnapshot;
  const snapshot = buildSnapshot(state);
  snapshotCache.set(taskId, snapshot);
  return snapshot;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useChatSessionSnapshot(taskId: number | null): ChatSessionSnapshot {
  return useSyncExternalStore(subscribe, () => getSnapshot(taskId), () => getSnapshot(taskId));
}

