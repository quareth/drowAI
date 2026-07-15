import { describe, expect, it } from "vitest";

import {
  computeDesiredTaskSubscriptions,
  planSubscriptionActions,
  planSubscriptionActionsFromInput,
} from "../TaskSubscriptionPlanner";

describe("TaskSubscriptionPlanner", () => {
  it("computes desired subscriptions with deterministic category priority", () => {
    const desired = computeDesiredTaskSubscriptions({
      runningTaskIds: [5, 3, 3, -1],
      activeTaskId: 2,
      pinnedTaskIds: [9, 5],
      pendingInterruptTaskIds: [7, 0, 9],
    });

    expect(desired).toEqual([2, 7, 9, 3, 5]);
  });

  it("caps desired subscriptions by maxSubscriptions while keeping priority", () => {
    const desired = computeDesiredTaskSubscriptions({
      runningTaskIds: [1, 2, 3],
      activeTaskId: 3,
      pinnedTaskIds: [9, 8],
      pendingInterruptTaskIds: [4],
      maxSubscriptions: 3,
    });

    expect(desired).toEqual([3, 4, 1]);
  });

  it("produces deterministic output regardless input ordering", () => {
    const first = planSubscriptionActionsFromInput([4, 2], {
      runningTaskIds: [3, 1],
      activeTaskId: 5,
      pinnedTaskIds: [7],
      pendingInterruptTaskIds: [6],
      maxSubscriptions: 3,
    });

    const second = planSubscriptionActionsFromInput([2, 4], {
      runningTaskIds: [1, 3],
      activeTaskId: 5,
      pinnedTaskIds: [7],
      pendingInterruptTaskIds: [6],
      maxSubscriptions: 3,
    });

    expect(first).toEqual(second);
  });

  it("subscribes in prioritized desired order", () => {
    const actions = planSubscriptionActionsFromInput([], {
      runningTaskIds: [1, 2],
      activeTaskId: 10,
      maxSubscriptions: 2,
    });

    expect(actions).toEqual([
      { type: "subscribe", taskId: 10 },
      { type: "subscribe", taskId: 1 },
    ]);
  });

  it("emits no duplicate actions when inputs contain duplicates", () => {
    const actions = planSubscriptionActions(
      [1, 1, 2, 2, 3],
      [2, 2, 4, 4, 4],
    );

    expect(actions).toEqual([
      { type: "subscribe", taskId: 4 },
      { type: "unsubscribe", taskId: 1 },
      { type: "unsubscribe", taskId: 3 },
    ]);
  });
});

