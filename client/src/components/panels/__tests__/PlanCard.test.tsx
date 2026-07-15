/**
 * PlanCard tests: plan approval/edit/reject flows and interrupt handling.
 */
// @vitest-environment jsdom
import { cleanup, render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";

import { PlanCard } from "@/components/panels/PlanCard";
import { PlanProvider, usePlanContext } from "@/contexts/PlanContext";

const mockResume = vi.fn();
const mockRefetch = vi.fn();
const mockSetInterrupt = vi.fn();

vi.mock("@/hooks/useGraphResume", () => ({
  useGraphResume: () => ({
    mutateAsync: mockResume,
    isPending: false,
  }),
}));

let interruptState = {
  interrupt: null as any,
  refetch: mockRefetch,
  setInterrupt: mockSetInterrupt,
};

vi.mock("@/hooks/useInterruptState", () => ({
  useInterruptState: () => interruptState,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

function PlanSeeder() {
  const { setPlan } = usePlanContext();
  useEffect(() => {
    setPlan(1, {
      type: "plan_review",
      goal: "Test goal",
      plan_steps: ["Step 1", "Step 2"],
      todo_list: [
        { id: "1", text: "Step 1", status: "pending" },
        { id: "2", text: "Step 2", status: "pending" },
      ],
    });
  }, [setPlan]);
  return null;
}

let latestContext: ReturnType<typeof usePlanContext> | null = null;

function PlanProbe() {
  latestContext = usePlanContext();
  return null;
}

afterEach(() => {
  cleanup();
  latestContext = null;
});

describe("PlanCard", () => {
  it("renders plan goal from context", () => {
    interruptState = { interrupt: null, refetch: mockRefetch, setInterrupt: mockSetInterrupt };
    render(
      <PlanProvider>
        <PlanSeeder />
        <PlanProbe />
        <PlanCard taskId={1} />
      </PlanProvider>,
    );

    expect(screen.getByText("Plan")).toBeTruthy();
  });

  it("shows approval actions when interrupted", () => {
    mockResume.mockClear();
    mockSetInterrupt.mockClear();
    interruptState = {
      interrupt: {
        taskId: 1,
        threadId: "thread-1",
        interruptId: "intr-plan-1",
        interruptType: "plan_review",
        graphName: "deep_reasoning",
        payload: {
          type: "plan_review",
          goal: "Interrupt goal",
          plan_steps: ["Step 1", "Step 2"],
          todo_list: [
            { id: "1", text: "Step 1", status: "pending" },
            { id: "2", text: "Step 2", status: "pending" },
          ],
        },
      },
      refetch: mockRefetch,
      setInterrupt: mockSetInterrupt,
    };

    render(
      <PlanProvider>
        <PlanProbe />
        <PlanCard taskId={1} />
      </PlanProvider>,
    );

    expect(screen.getByText("Run")).toBeTruthy();
    expect(screen.getByText("Edit")).toBeTruthy();
    expect(screen.getByText("Reject")).toBeTruthy();
  });

  it("submits approval on run click", async () => {
    mockResume.mockClear();
    mockSetInterrupt.mockClear();
    interruptState = {
      interrupt: {
        taskId: 1,
        threadId: "thread-1",
        interruptId: "intr-plan-1",
        interruptType: "plan_review",
        graphName: "deep_reasoning",
        payload: {
          type: "plan_review",
          goal: "Interrupt goal",
          plan_steps: ["Step 1", "Step 2"],
          todo_list: [
            { id: "1", text: "Step 1", status: "pending" },
            { id: "2", text: "Step 2", status: "pending" },
          ],
        },
      },
      refetch: mockRefetch,
      setInterrupt: mockSetInterrupt,
    };

    render(
      <PlanProvider>
        <PlanProbe />
        <PlanCard taskId={1} />
      </PlanProvider>,
    );

    fireEvent.click(screen.getAllByText("Run")[0]!);

    expect(mockResume).toHaveBeenCalledWith(
      expect.objectContaining({
        taskId: 1,
        interruptType: "plan_review",
        interruptId: "intr-plan-1",
        graphName: "deep_reasoning",
        response: { action: "approve" },
      }),
    );
  });

  it("submits edited plan steps on save and run", async () => {
    mockResume.mockClear();
    mockSetInterrupt.mockClear();
    interruptState = {
      interrupt: {
        taskId: 1,
        threadId: "thread-1",
        interruptId: "intr-plan-1",
        interruptType: "plan_review",
        graphName: "deep_reasoning",
        payload: {
          type: "plan_review",
          goal: "Interrupt goal",
          plan_steps: ["Collect evidence", "Summarize findings"],
          todo_list: [
            { id: "1", text: "Collect evidence", status: "pending" },
            { id: "2", text: "Summarize findings", status: "pending" },
          ],
        },
      },
      refetch: mockRefetch,
      setInterrupt: mockSetInterrupt,
    };

    render(
      <PlanProvider>
        <PlanProbe />
        <PlanCard taskId={1} />
      </PlanProvider>,
    );

    fireEvent.click(screen.getByText("Edit"));
    fireEvent.click(screen.getByLabelText("Edit step 1"));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Collect scoped evidence" },
    });
    fireEvent.click(screen.getByText("Save & Run"));

    await waitFor(() => {
      expect(mockResume).toHaveBeenCalledWith(
        expect.objectContaining({
          taskId: 1,
          interruptType: "plan_review",
          interruptId: "intr-plan-1",
          graphName: "deep_reasoning",
          response: {
            action: "edit",
            edited_plan_steps: [
              "Step 1: Collect scoped evidence",
              "Step 2: Summarize findings",
            ],
          },
        }),
      );
    });
    expect(mockSetInterrupt).toHaveBeenCalledWith(null);
  });

  it("submits rejection without changing the interrupt contract", async () => {
    mockResume.mockClear();
    mockSetInterrupt.mockClear();
    interruptState = {
      interrupt: {
        taskId: 1,
        threadId: "thread-1",
        interruptId: "intr-plan-1",
        interruptType: "plan_review",
        graphName: "deep_reasoning",
        payload: {
          type: "plan_review",
          goal: "Interrupt goal",
          plan_steps: ["Collect evidence", "Summarize findings"],
          todo_list: [
            { id: "1", text: "Collect evidence", status: "pending" },
            { id: "2", text: "Summarize findings", status: "pending" },
          ],
        },
      },
      refetch: mockRefetch,
      setInterrupt: mockSetInterrupt,
    };

    render(
      <PlanProvider>
        <PlanProbe />
        <PlanCard taskId={1} />
      </PlanProvider>,
    );

    fireEvent.click(screen.getByText("Reject"));

    await waitFor(() => {
      expect(mockResume).toHaveBeenCalledWith(
        expect.objectContaining({
          taskId: 1,
          interruptType: "plan_review",
          interruptId: "intr-plan-1",
          graphName: "deep_reasoning",
          response: { action: "reject" },
        }),
      );
    });
    expect(mockSetInterrupt).toHaveBeenCalledWith(null);
  });

  it("does not apply optimistic todo progression before backend updates", () => {
    mockResume.mockClear();
    mockSetInterrupt.mockClear();
    interruptState = {
      interrupt: {
        taskId: 1,
        threadId: "thread-1",
        interruptId: "intr-plan-1",
        interruptType: "plan_review",
        graphName: "deep_reasoning",
        payload: {
          type: "plan_review",
          goal: "Interrupt goal",
          plan_steps: ["Step 1", "Step 2"],
          todo_list: [
            { id: "1", text: "Step 1", status: "pending" },
            { id: "2", text: "Step 2", status: "pending" },
          ],
        },
      },
      refetch: mockRefetch,
      setInterrupt: mockSetInterrupt,
    };

    render(
      <PlanProvider>
        <PlanProbe />
        <PlanCard taskId={1} />
      </PlanProvider>,
    );

    const before = latestContext?.getTaskState(1).currentRun?.todoList ?? [];
    expect(before.map((todo) => todo.status)).toEqual(["pending", "pending"]);

    fireEvent.click(screen.getAllByText("Run")[0]!);

    const after = latestContext?.getTaskState(1).currentRun?.todoList ?? [];
    expect(after.map((todo) => todo.status)).toEqual(["pending", "pending"]);
    expect(mockSetInterrupt).toHaveBeenCalledWith(null);
  });

  it("prefers authoritative run progress over stale interrupt payload", async () => {
    mockResume.mockClear();
    mockSetInterrupt.mockClear();
    interruptState = {
      interrupt: {
        taskId: 1,
        threadId: "thread-1",
        interruptId: "intr-plan-1",
        interruptType: "plan_review",
        graphName: "deep_reasoning",
        payload: {
          type: "plan_review",
          goal: "Interrupt goal",
          plan_steps: ["Step 1", "Step 2"],
          todo_list: [
            { id: "1", text: "Step 1", status: "in_progress" },
            { id: "2", text: "Step 2", status: "pending" },
          ],
        },
      },
      refetch: mockRefetch,
      setInterrupt: mockSetInterrupt,
    };

    render(
      <PlanProvider>
        <PlanProbe />
        <PlanCard taskId={1} />
      </PlanProvider>,
    );

    act(() => {
      latestContext?.setPlan(1, {
        type: "plan_review",
        goal: "Interrupt goal",
        plan_steps: ["Step 1", "Step 2"],
        todo_list: [
          { id: "1", text: "Step 1", status: "in_progress" },
          { id: "2", text: "Step 2", status: "pending" },
        ],
      });
      latestContext?.applyTodoUpdates(1, [
        { id: "1", index: 0, status: "completed" },
        { id: "2", index: 1, status: "in_progress" },
      ]);
    });

    await waitFor(() => {
      expect(latestContext?.getTaskState(1).currentRun?.status).toBe("executing");
    });
    expect(screen.queryByText("Run")).toBeNull();
    expect(latestContext?.getTaskState(1).currentRun?.status).toBe("executing");
    expect(
      latestContext?.getTaskState(1).currentRun?.todoList.map((todo) => todo.status),
    ).toEqual(["completed", "in_progress"]);
  });
});
