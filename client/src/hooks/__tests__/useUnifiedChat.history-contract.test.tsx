// @vitest-environment jsdom
import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useUnifiedChat } from "@/hooks/useUnifiedChat";
import type { ModeOrchestrationContract } from "@/components/chat/mode-orchestration";

const mocked = vi.hoisted(() => ({
  orchestrateMessageFlow: vi.fn(async () => {}),
  fetchOlderTranscriptPage: vi.fn(async () => ({
    contractVersion: "2026-03-01.chat-history.v2",
    items: [],
    nextBeforeTurn: null,
    hasMoreOlder: false,
  })),
  normalizeTranscriptItemsToSteps: vi.fn(() => []),
  tryStartHistoryLoading: vi.fn(() => true),
  setHistoryLoading: vi.fn(),
  setTaskHistory: vi.fn(),
  setTranscriptPaginationState: vi.fn(),
  streamSnapshot: {
    items: [],
    isConnected: false,
    connectionError: null,
    hasMoreOlderByConversation: { "__default__": false },
    nextBeforeCursorByConversation: { "__default__": null },
  },
  sessionSnapshot: {
    conversationId: null,
  },
}));

vi.mock("@/hooks/useOptimisticUpdates", () => ({
  useOptimisticUpdates: () => ({
    messages: [],
    addOptimisticMessage: vi.fn(),
    failMessage: vi.fn(),
    clearMessage: vi.fn(),
  }),
}));

vi.mock("@/utils/chatFilters", () => ({
  filterChatMessages: <T,>(items: T[]) => items,
}));

vi.mock("@/utils/stepToChatMessage", () => ({
  stepToChatMessage: (step: unknown) => step,
}));

vi.mock("@/config/feature-flags", () => ({
  featureFlags: {
    enableOptimisticUpdates: false,
    enableUnifiedChatFilters: false,
  },
}));

vi.mock("@/hooks/chat-history-bootstrap", () => ({
  HISTORY_FETCH_TIMEOUT_MS: 30_000,
  fetchOlderTranscriptPage: mocked.fetchOlderTranscriptPage,
  normalizeTranscriptItemsToSteps: mocked.normalizeTranscriptItemsToSteps,
}));

vi.mock("@/state/chat-stream-store", () => ({
  useTaskStreamSnapshot: () => mocked.streamSnapshot,
  tryStartHistoryLoading: mocked.tryStartHistoryLoading,
  setHistoryLoading: mocked.setHistoryLoading,
  setTaskHistory: mocked.setTaskHistory,
  setTranscriptPaginationState: mocked.setTranscriptPaginationState,
}));

vi.mock("@/state/chat-session-store", () => ({
  setChatState: vi.fn(),
  useChatSessionSnapshot: () => mocked.sessionSnapshot,
}));

const orchestrator: ModeOrchestrationContract = {
  mode: "interactive",
  sseConnection: {
    isConnected: false,
    reconnect: vi.fn(),
    disconnect: vi.fn(),
  },
  setStrategy: vi.fn(),
  orchestrateMessageFlow: mocked.orchestrateMessageFlow,
  handleSSEReconnect: vi.fn(async () => {}),
  validateModeTransition: vi.fn(() => true),
};

function Harness({ taskId, onProvider }: { taskId: number | null; onProvider?: (provider: ReturnType<typeof useUnifiedChat>) => void }) {
  const provider = useUnifiedChat({
    taskId,
    orchestrator,
  });
  onProvider?.(provider);
  return null;
}

describe("useUnifiedChat history contract", () => {
  afterEach(() => {
    vi.clearAllMocks();
    mocked.streamSnapshot = {
      items: [],
      isConnected: false,
      connectionError: null,
      hasMoreOlderByConversation: { "__default__": false },
      nextBeforeCursorByConversation: { "__default__": null },
    };
    mocked.sessionSnapshot = {
      conversationId: null,
    };
  });

  it("does not fetch history during mount", async () => {
    render(<Harness taskId={4512} />);

    await waitFor(() => {
      expect(mocked.orchestrateMessageFlow).not.toHaveBeenCalled();
    });
  });

  it("keeps send orchestration path intact", async () => {
    let provider: ReturnType<typeof useUnifiedChat> | null = null;
    render(<Harness taskId={4513} onProvider={(value) => { provider = value; }} />);

    await waitFor(() => {
      expect(provider).not.toBeNull();
    });

    await provider!.sendMessage("hello world");
    expect(mocked.orchestrateMessageFlow).toHaveBeenCalledWith("hello world", "interactive");
  });

  it("uses transcript cursor for loadMore and exposes store-driven hasMore", async () => {
    mocked.streamSnapshot = {
      ...mocked.streamSnapshot,
      hasMoreOlderByConversation: { "__default__": true },
      nextBeforeCursorByConversation: { "__default__": 42 },
    };
    let provider: ReturnType<typeof useUnifiedChat> | null = null;
    render(<Harness taskId={9001} onProvider={(value) => { provider = value; }} />);

    await waitFor(() => {
      expect(provider).not.toBeNull();
      expect(provider!.hasMore).toBe(true);
    });

    await provider!.loadMore();

    expect(mocked.tryStartHistoryLoading).toHaveBeenCalledWith(9001, null);
    expect(mocked.fetchOlderTranscriptPage).toHaveBeenCalledWith(
      9001,
      expect.objectContaining({ beforeTurn: 42, conversationId: null }),
    );
    expect(mocked.setTranscriptPaginationState).toHaveBeenCalledWith(
      9001,
      expect.objectContaining({ conversationId: null, hasMoreOlder: false, nextBeforeCursor: null }),
    );
    expect(mocked.setHistoryLoading).toHaveBeenCalledWith(9001, false, null);
  });

  it("skips loadMore when a history load is already in-flight", async () => {
    mocked.streamSnapshot = {
      ...mocked.streamSnapshot,
      hasMoreOlderByConversation: { "__default__": true },
      nextBeforeCursorByConversation: { "__default__": 11 },
    };
    mocked.tryStartHistoryLoading.mockReturnValueOnce(false);
    let provider: ReturnType<typeof useUnifiedChat> | null = null;
    render(<Harness taskId={9002} onProvider={(value) => { provider = value; }} />);

    await waitFor(() => {
      expect(provider).not.toBeNull();
    });

    await provider!.loadMore();

    expect(mocked.fetchOlderTranscriptPage).not.toHaveBeenCalled();
    expect(mocked.setHistoryLoading).not.toHaveBeenCalled();
  });
});
