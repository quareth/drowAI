/**
 * Regression tests for runtime stream bootstrap subscription planning.
 *
 * Ensures stopped tasks are not subscribed only because they are selected in
 * chat, while active runtime tasks continue to be included.
 */
// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import { RuntimeStreamBootstrap } from "@/components/runtime/RuntimeStreamBootstrap";

const mocked = vi.hoisted(() => ({
  useAuth: vi.fn(),
  useActiveChatTaskId: vi.fn(),
  useQuery: vi.fn(),
  useMultiTaskStreamManager: vi.fn(),
}));

vi.mock("@/hooks/use-auth", () => ({
  useAuth: mocked.useAuth,
}));

vi.mock("@/state/active-chat-task-store", () => ({
  useActiveChatTaskId: mocked.useActiveChatTaskId,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: mocked.useQuery,
}));

vi.mock("@/hooks/useMultiTaskStreamManager", () => ({
  useMultiTaskStreamManager: mocked.useMultiTaskStreamManager,
}));

describe("RuntimeStreamBootstrap", () => {
  beforeEach(() => {
    mocked.useMultiTaskStreamManager.mockReset();
    mocked.useAuth.mockReturnValue({ user: { id: 1 } });
    mocked.useActiveChatTaskId.mockReturnValue(null);
    mocked.useQuery.mockReturnValue({ data: [] });
  });

  it("excludes stopped active chat task from stream subscriptions", () => {
    mocked.useActiveChatTaskId.mockReturnValue(22);
    mocked.useQuery.mockReturnValue({
      data: [
        { id: 22, status: "stopped" },
        { id: 7, status: "running" },
      ],
    });

    render(<RuntimeStreamBootstrap />);

    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledTimes(1);
    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        taskIds: [7],
      }),
    );
  });

  it("keeps active runtime task subscribed", () => {
    mocked.useActiveChatTaskId.mockReturnValue(11);
    mocked.useQuery.mockReturnValue({
      data: [
        { id: 11, status: "running" },
        { id: 7, status: "waiting_for_human" },
      ],
    });

    render(<RuntimeStreamBootstrap />);

    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledTimes(1);
    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        taskIds: [11, 7],
      }),
    );
  });

  it("subscribes active chat task while runtime is bootstrapping", () => {
    mocked.useActiveChatTaskId.mockReturnValue(31);
    mocked.useQuery.mockReturnValue({
      data: [
        { id: 31, status: "starting" },
        { id: 32, status: "queued" },
        { id: 7, status: "running" },
      ],
    });

    render(<RuntimeStreamBootstrap />);

    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledTimes(1);
    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        taskIds: [31, 7],
      }),
    );
  });

  it("subscribes queued active chat task during create-to-start race", () => {
    mocked.useActiveChatTaskId.mockReturnValue(41);
    mocked.useQuery.mockReturnValue({
      data: [
        { id: 41, status: "queued" },
      ],
    });

    render(<RuntimeStreamBootstrap />);

    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledTimes(1);
    expect(mocked.useMultiTaskStreamManager).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        taskIds: [41],
      }),
    );
  });
});
