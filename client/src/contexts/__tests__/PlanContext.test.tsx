// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { createRoot } from "react-dom/client";
import { act } from "react-dom/test-utils";

import { PlanProvider, usePlanContext } from "@/contexts/PlanContext";

let latestContext: ReturnType<typeof usePlanContext> | null = null;

function PlanProbe() {
  const context = usePlanContext();
  latestContext = context;
  return null;
}

function renderWithPlanProvider() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);

  act(() => {
    root.render(
      <PlanProvider>
        <PlanProbe />
      </PlanProvider>,
    );
  });

  return { root, container };
}

describe("PlanContext", () => {
  it("sets plan and tracks progress", () => {
    const { root, container } = renderWithPlanProvider();

    act(() => {
      latestContext?.setPlan(1, {
        type: "plan_review",
        goal: "Test goal",
        plan_steps: ["Step 1"],
        todo_list: [{ id: "1", text: "Todo", status: "pending" }],
      });
    });

    expect(latestContext?.getTaskState(1).currentRun?.goal).toBe("Test goal");
    expect(latestContext?.hasActivePlan(1)).toBe(true);

    act(() => {
      latestContext?.updateTodo(1, "1", "completed");
    });

    expect(latestContext?.getTodoProgress(1).percent).toBe(100);

    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("applies explicit todo updates without implicit progression inference", () => {
    const { root, container } = renderWithPlanProvider();

    act(() => {
      latestContext?.setPlan(7, {
        type: "plan_review",
        goal: "Inference guard",
        plan_steps: ["Step 1", "Step 2", "Step 3"],
        todo_list: [
          { id: "1", text: "Step 1", status: "pending" },
          { id: "2", text: "Step 2", status: "pending" },
          { id: "3", text: "Step 3", status: "pending" },
        ],
      });
    });

    act(() => {
      latestContext?.applyTodoUpdates(7, [{ id: "2", status: "completed" }]);
    });

    const todos = latestContext?.getTaskState(7).currentRun?.todoList ?? [];
    expect(todos[0]?.status).toBe("pending");
    expect(todos[1]?.status).toBe("completed");
    expect(todos[2]?.status).toBe("pending");

    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("applies multi-step authoritative updates in a single event", () => {
    const { root, container } = renderWithPlanProvider();

    act(() => {
      latestContext?.setPlan(9, {
        type: "plan_review",
        goal: "Multi-update",
        plan_steps: ["Step 1", "Step 2", "Step 3"],
        todo_list: [
          { id: "1", text: "Step 1", status: "in_progress" },
          { id: "2", text: "Step 2", status: "pending" },
          { id: "3", text: "Step 3", status: "pending" },
        ],
      });
    });

    act(() => {
      latestContext?.applyTodoUpdates(9, [
        { id: "1", status: "completed" },
        { id: "2", status: "completed" },
        { id: "3", status: "in_progress" },
      ]);
    });

    const todos = latestContext?.getTaskState(9).currentRun?.todoList ?? [];
    expect(todos.map((todo) => todo.status)).toEqual([
      "completed",
      "completed",
      "in_progress",
    ]);

    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("ingests task-plan-created stream events into current run", () => {
    const { root, container } = renderWithPlanProvider();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-plan-created", {
          detail: {
            taskId: 55,
            goal: "Streamed goal",
            planSteps: ["Step 1", "Step 2"],
            todoList: [
              { id: "1", text: "Step 1", status: "in_progress" },
              { id: "2", text: "Step 2", status: "pending" },
            ],
            runId: 5,
            planVersion: 2,
            sequence: 100,
          },
        }),
      );
    });

    const currentRun = latestContext?.getTaskState(55).currentRun;
    expect(currentRun?.runId).toBe(5);
    expect(currentRun?.goal).toBe("Streamed goal");
    expect(currentRun?.planVersion).toBe(2);
    expect(currentRun?.status).toBe("executing");
    expect(currentRun?.todoList.map((todo) => todo.status)).toEqual([
      "in_progress",
      "pending",
    ]);

    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("applies task-todo-progress events and ignores stale or mismatched updates", () => {
    const { root, container } = renderWithPlanProvider();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-plan-created", {
          detail: {
            taskId: 88,
            goal: "Streamed run",
            planSteps: ["Step 1", "Step 2"],
            todoList: [
              { id: "1", text: "Step 1", status: "in_progress" },
              { id: "2", text: "Step 2", status: "pending" },
            ],
            runId: 7,
            planVersion: 4,
            sequence: 200,
          },
        }),
      );
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-todo-progress", {
          detail: {
            taskId: 88,
            runId: 7,
            planVersion: 4,
            sequence: 201,
            updates: [
              { id: "1", status: "completed", index: 0, plan_version: 4 },
              { id: "2", status: "in_progress", index: 1, plan_version: 4 },
            ],
          },
        }),
      );
    });

    expect(
      latestContext?.getTaskState(88).currentRun?.todoList.map((todo) => todo.status),
    ).toEqual(["completed", "in_progress"]);

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-todo-progress", {
          detail: {
            taskId: 88,
            runId: 7,
            planVersion: 4,
            sequence: 200,
            updates: [{ id: "2", status: "completed", index: 1, plan_version: 4 }],
          },
        }),
      );
    });

    expect(
      latestContext?.getTaskState(88).currentRun?.todoList.map((todo) => todo.status),
    ).toEqual(["completed", "in_progress"]);

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-todo-progress", {
          detail: {
            taskId: 88,
            runId: 8,
            planVersion: 4,
            sequence: 202,
            updates: [{ id: "2", status: "completed", index: 1, plan_version: 4 }],
          },
        }),
      );
    });

    expect(
      latestContext?.getTaskState(88).currentRun?.todoList.map((todo) => todo.status),
    ).toEqual(["completed", "in_progress"]);

    act(() => {
      window.dispatchEvent(
        new CustomEvent("task-todo-progress", {
          detail: {
            taskId: 88,
            runId: 7,
            planVersion: 5,
            sequence: 203,
            updates: [{ id: "2", status: "completed", index: 1, plan_version: 5 }],
          },
        }),
      );
    });

    expect(
      latestContext?.getTaskState(88).currentRun?.todoList.map((todo) => todo.status),
    ).toEqual(["completed", "in_progress"]);

    act(() => {
      root.unmount();
    });
    container.remove();
  });
});
