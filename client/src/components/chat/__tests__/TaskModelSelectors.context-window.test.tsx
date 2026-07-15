// @vitest-environment jsdom
/**
 * Verifies TaskModelSelectors context-window indicator rendering behavior.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TaskModelSelectors } from "@/components/chat/TaskModelSelectors";

const mocked = vi.hoisted(() => ({
  apiCallMock: vi.fn(async () => null),
  useContextWindowMock: vi.fn(),
  refreshContextMock: vi.fn(),
}));

vi.mock("@/lib/api-config", () => ({
  apiCall: mocked.apiCallMock,
}));

vi.mock("@/hooks/useContextWindow", () => ({
  useContextWindow: mocked.useContextWindowMock,
}));

afterEach(() => {
  cleanup();
  mocked.apiCallMock.mockReset();
  mocked.useContextWindowMock.mockReset();
  mocked.refreshContextMock.mockReset();
});

function renderSelectors(props: Partial<ComponentProps<typeof TaskModelSelectors>> = {}) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <TaskModelSelectors
        selectedTaskId={11}
        conversationId="conv-a"
        onTaskChange={() => undefined}
        selectedSelection={{ provider: "openai", model: "gpt-5-mini" }}
        onModelChange={() => undefined}
        tasks={[
          {
            id: 11,
            name: "Task Eleven",
            status: "running",
            created_at: new Date().toISOString(),
          } as any,
        ]}
        llmCatalog={{
          providers: [
            {
              id: "openai",
              label: "OpenAI",
              capabilities: [],
              available: true,
              selectable: true,
              credential: {
                user_id: 1,
                provider: "openai",
                enabled: true,
                has_api_key: true,
                masked_api_key: "sk-...1234",
              },
              defaultModel: "gpt-5-mini",
              models: [{ id: "gpt-5-mini", label: "GPT-5 mini" }],
            },
          ],
        }}
        isConnected
        runStates={{}}
        {...props}
      />
    </QueryClientProvider>,
  );
}

describe("TaskModelSelectors context indicator", () => {
  it("labels a provider refusal as declined", () => {
    mocked.useContextWindowMock.mockReturnValue({
      snapshot: {
        taskId: 11,
        conversationId: "conv-a",
        maxTokens: 1000,
        usedTokens: 100,
        remainingTokens: 900,
        ratio: 0.1,
        ceilingReached: false,
        recommendedNextAction: "none",
        compressionCandidate: false,
      },
      refresh: mocked.refreshContextMock,
    });

    renderSelectors({
      runStates: {
        11: {
          state: "declined",
          turnId: "task-11-turn-1",
          cancelRequested: false,
          isStreaming: false,
          queuedCount: 0,
          isActiveGeneration: false,
          canStop: false,
        },
      },
    });

    expect(screen.getAllByText("Task Eleven (#11) - declined")).toHaveLength(2);
  });

  it("renders a circular indicator with default max-token tooltip semantics", () => {
    mocked.refreshContextMock.mockReset();
    mocked.useContextWindowMock.mockReturnValue({
      snapshot: {
        taskId: 11,
        conversationId: "conv-a",
        maxTokens: 0,
        usedTokens: 64000,
        remainingTokens: 64000,
        ratio: 0.5,
        ceilingReached: false,
        recommendedNextAction: "none",
        compressionCandidate: false,
      },
      refresh: mocked.refreshContextMock,
    });

    renderSelectors();

    const indicator = screen.getByLabelText("Context window usage 50%");
    expect(indicator).toBeTruthy();
    fireEvent.mouseEnter(indicator);
    expect(screen.getByText("Context window: 50%")).toBeTruthy();
    fireEvent.mouseLeave(indicator);
  });

  it("updates indicator when task and conversation selection changes", () => {
    mocked.refreshContextMock.mockReset();
    mocked.useContextWindowMock.mockImplementation(
      ({ taskId, conversationId }: { taskId: number | null; conversationId?: string | null }) => {
        if (taskId === 22 && conversationId === "conv-b") {
          return {
            snapshot: {
              taskId: 22,
              conversationId: "conv-b",
              maxTokens: 1000,
              usedTokens: 900,
              remainingTokens: 100,
              ratio: 0.9,
              ceilingReached: false,
              recommendedNextAction: "none",
              compressionCandidate: false,
            },
            refresh: mocked.refreshContextMock,
          };
        }
        return {
          snapshot: {
            taskId: 11,
            conversationId: "conv-a",
            maxTokens: 1000,
            usedTokens: 200,
            remainingTokens: 800,
            ratio: 0.2,
            ceilingReached: false,
            recommendedNextAction: "none",
            compressionCandidate: false,
          },
          refresh: mocked.refreshContextMock,
        };
      },
    );

    const view = renderSelectors();
    expect(screen.getByLabelText("Context window usage 20%")).toBeTruthy();

    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <TaskModelSelectors
          selectedTaskId={22}
          conversationId="conv-b"
          onTaskChange={() => undefined}
          selectedSelection={{ provider: "openai", model: "gpt-5-mini" }}
          onModelChange={() => undefined}
          tasks={[
            {
              id: 22,
              name: "Task Twenty Two",
              status: "running",
              created_at: new Date().toISOString(),
            } as any,
          ]}
          llmCatalog={{
            providers: [
              {
                id: "openai",
                label: "OpenAI",
                capabilities: [],
                available: true,
                selectable: true,
                credential: {
                  user_id: 1,
                  provider: "openai",
                  enabled: true,
                  has_api_key: true,
                  masked_api_key: "sk-...1234",
                },
                defaultModel: "gpt-5-mini",
                models: [{ id: "gpt-5-mini", label: "GPT-5 mini" }],
              },
            ],
          }}
          isConnected
          runStates={{}}
        />
      </QueryClientProvider>,
    );

    const indicator = screen.getByLabelText("Context window usage 90%");
    fireEvent.mouseEnter(indicator);
    expect(screen.getByText("Context window: 90%")).toBeTruthy();
    fireEvent.mouseLeave(indicator);
  });

  it("requests a fresh context snapshot when hovering the context indicator", () => {
    mocked.refreshContextMock.mockReset();
    mocked.useContextWindowMock.mockReturnValue({
      snapshot: {
        taskId: 11,
        conversationId: "conv-a",
        maxTokens: 1000,
        usedTokens: 500,
        remainingTokens: 500,
        ratio: 0.5,
        ceilingReached: false,
        recommendedNextAction: "none",
        compressionCandidate: false,
      },
      refresh: mocked.refreshContextMock,
    });

    renderSelectors();
    const indicator = screen.getByLabelText("Context window usage 50%");
    fireEvent.mouseEnter(indicator);

    expect(mocked.refreshContextMock).toHaveBeenCalledTimes(1);
    expect(mocked.refreshContextMock).toHaveBeenCalledWith({ force: false });
  });

  it("calls the transcript download handler from the header button", () => {
    mocked.useContextWindowMock.mockReturnValue({
      snapshot: {
        taskId: 11,
        conversationId: "conv-a",
        maxTokens: 1000,
        usedTokens: 100,
        remainingTokens: 900,
        ratio: 0.1,
        ceilingReached: false,
        recommendedNextAction: "none",
        compressionCandidate: false,
      },
      refresh: mocked.refreshContextMock,
    });
    const onDownloadTranscript = vi.fn();

    renderSelectors({ onDownloadTranscript });
    fireEvent.click(screen.getByLabelText("Download transcript"));

    expect(onDownloadTranscript).toHaveBeenCalledTimes(1);
  });

  it("disables transcript download while export is pending", () => {
    mocked.useContextWindowMock.mockReturnValue({
      snapshot: {
        taskId: 11,
        conversationId: "conv-a",
        maxTokens: 1000,
        usedTokens: 100,
        remainingTokens: 900,
        ratio: 0.1,
        ceilingReached: false,
        recommendedNextAction: "none",
        compressionCandidate: false,
      },
      refresh: mocked.refreshContextMock,
    });

    renderSelectors({
      onDownloadTranscript: vi.fn(),
      isTranscriptDownloadPending: true,
    });

    expect(screen.getByLabelText("Download transcript")).toHaveProperty("disabled", true);
  });

  it("shows a clear non-runnable selection status", () => {
    mocked.useContextWindowMock.mockReturnValue({
      snapshot: {
        taskId: 11,
        conversationId: "conv-a",
        maxTokens: 1000,
        usedTokens: 100,
        remainingTokens: 900,
        ratio: 0.1,
        ceilingReached: false,
        recommendedNextAction: "none",
        compressionCandidate: false,
      },
      refresh: mocked.refreshContextMock,
    });

    renderSelectors({
      selectionStatus: {
        status: "adapter_unavailable",
        selectable: false,
        runnable: false,
        reason: "LLM provider adapter is not registered: anthropic",
      },
      selectedSelection: { provider: "anthropic", model: "claude-sonnet-4-6" },
    });

    expect(screen.getByRole("alert").textContent).toContain("Provider unavailable");
    expect(screen.getByRole("alert").textContent).toContain("adapter is not registered");
  });

  it("refreshes token usage on usage-hover without polling", async () => {
    mocked.apiCallMock.mockReset();
    mocked.apiCallMock.mockResolvedValue({
      task_id: 11,
      prompt_tokens: 100,
      completion_tokens: 50,
      total_tokens: 150,
      cached_tokens: 0,
      reasoning_tokens: 0,
      cost_usd: 0.0015,
      call_count: 1,
      updated_at: new Date().toISOString(),
    });
    mocked.useContextWindowMock.mockReturnValue({
      snapshot: {
        taskId: 11,
        conversationId: "conv-a",
        maxTokens: 1000,
        usedTokens: 100,
        remainingTokens: 900,
        ratio: 0.1,
        ceilingReached: false,
        recommendedNextAction: "none",
        compressionCandidate: false,
      },
      refresh: mocked.refreshContextMock,
    });

    renderSelectors();

    await waitFor(() => {
      expect(mocked.apiCallMock).toHaveBeenCalledTimes(1);
    });

    fireEvent.mouseEnter(screen.getByLabelText("Task usage metrics"));

    await waitFor(() => {
      expect(mocked.apiCallMock).toHaveBeenCalledTimes(2);
    });
  });
});
