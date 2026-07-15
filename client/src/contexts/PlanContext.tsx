/**
 * Plan state context for plan-review interruptions and live todo progression updates.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";

import type { PlanReviewPayload, TodoItem } from "@/types/hitl";

export interface PlanRun {
  runId: number;
  timestamp: string;
  goal: string;
  planSteps: string[];
  todoList: TodoItem[];
  planVersion: number;
  status: "planning" | "executing" | "completed" | "interrupted" | "rejected";
  reasoning?: string;
}

interface TaskPlanState {
  currentRun: PlanRun | null;
  runHistory: PlanRun[];
}

interface TaskPlanUiState {
  isPlanCardMinimized: boolean;
  isTodoCardMinimized: boolean;
}

interface PlanState {
  runsByTask: Record<number, TaskPlanState>;
  uiByTask: Record<number, TaskPlanUiState>;
  activeTaskId: number | null;
}

interface StreamPlanCreatedEventDetail {
  taskId: number;
  goal: string;
  planSteps: string[];
  todoList: Array<{ id: string; text: string; status: TodoItem["status"] }>;
  runId?: number;
  planVersion?: number;
  sequence?: number;
}

interface StreamTodoProgressEventDetail {
  taskId: number;
  updates: Array<{ id: string; status: TodoItem["status"]; text?: string; index?: number; plan_version?: number }>;
  runId?: number;
  planVersion?: number;
  sequence?: number;
}

type PlanAction =
  | { type: "SET_PLAN"; taskId: number; payload: PlanReviewPayload }
  | { type: "UPDATE_PLAN"; taskId: number; goal?: string; planSteps?: string[]; planVersion?: number }
  | { type: "UPDATE_TODO"; taskId: number; todoId: string; status: TodoItem["status"] }
  | { type: "APPLY_TODO_UPDATES"; taskId: number; updates: Array<{ id: string; status: TodoItem["status"]; text?: string; index?: number; plan_version?: number }> }
  | { type: "INGEST_STREAM_PLAN_CREATED"; payload: StreamPlanCreatedEventDetail }
  | { type: "INGEST_STREAM_TODO_PROGRESS"; payload: StreamTodoProgressEventDetail }
  | { type: "START_NEW_RUN"; taskId: number; runId: number }
  | { type: "COMPLETE_RUN"; taskId: number }
  | { type: "REJECT_RUN"; taskId: number }
  | { type: "SET_ACTIVE_TASK"; taskId: number | null }
  | { type: "SET_PLAN_CARD_MINIMIZED"; taskId: number; minimized: boolean }
  | { type: "SET_TODO_CARD_MINIMIZED"; taskId: number; minimized: boolean }
  | { type: "CLEAR_STATE"; taskId?: number | null };

const initialState: PlanState = {
  runsByTask: {},
  uiByTask: {},
  activeTaskId: null,
};

const STEP_PREFIX_PATTERN = /^step\s+\d+\s*[:.-]?\s*/i;

function normalizeTodoText(text: string): string {
  return text.replace(STEP_PREFIX_PATTERN, "").trim().toLowerCase();
}

function getTaskState(state: PlanState, taskId: number): TaskPlanState {
  return state.runsByTask[taskId] ?? { currentRun: null, runHistory: [] };
}

function getTaskUiState(state: PlanState, taskId: number): TaskPlanUiState {
  return state.uiByTask[taskId] ?? { isPlanCardMinimized: false, isTodoCardMinimized: false };
}

function updateTaskState(
  state: PlanState,
  taskId: number,
  updater: (current: TaskPlanState) => TaskPlanState,
  options?: { setActiveTask?: boolean },
): PlanState {
  const current = getTaskState(state, taskId);
  const updated = updater(current);
  return {
    ...state,
    runsByTask: {
      ...state.runsByTask,
      [taskId]: updated,
    },
    activeTaskId: options?.setActiveTask === false ? state.activeTaskId : taskId,
  };
}

function normalizeTodoStatus(value: unknown): TodoItem["status"] {
  const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (normalized === "in_progress") return "in_progress";
  if (normalized === "completed") return "completed";
  if (normalized === "skipped") return "skipped";
  return "pending";
}

function normalizePlanSteps(rawPlanSteps: unknown[]): string[] {
  return rawPlanSteps
    .map((step) => (typeof step === "string" ? step.trim() : ""))
    .filter((step) => step.length > 0);
}

function normalizeTodoList(
  rawTodoList: unknown[],
  fallbackSteps: string[],
): Array<{ id: string; text: string; status: TodoItem["status"] }> {
  return rawTodoList.map((rawTodo, index) => {
    const todo = rawTodo && typeof rawTodo === "object" ? (rawTodo as Record<string, unknown>) : {};
    const fallbackText = fallbackSteps[index] ?? `Step ${index + 1}`;
    const text = typeof todo.text === "string" && todo.text.trim().length > 0 ? todo.text : fallbackText;
    const id = typeof todo.id === "string" && todo.id.trim().length > 0 ? todo.id : `${index + 1}`;
    return {
      id,
      text,
      status: normalizeTodoStatus(todo.status),
    };
  });
}

function normalizeTodoUpdates(
  rawUpdates: unknown[],
  fallbackPlanVersion?: number,
): Array<{ id: string; status: TodoItem["status"]; text?: string; index?: number; plan_version?: number }> {
  const updates: Array<{
    id: string;
    status: TodoItem["status"];
    text?: string;
    index?: number;
    plan_version?: number;
  }> = [];
  for (const rawUpdate of rawUpdates) {
    if (!rawUpdate || typeof rawUpdate !== "object") {
      continue;
    }
    const update = rawUpdate as Record<string, unknown>;
    updates.push({
      id: typeof update.id === "string" ? update.id : "",
      status: normalizeTodoStatus(update.status),
      text: typeof update.text === "string" ? update.text : undefined,
      index:
        typeof update.index === "number" && Number.isFinite(update.index)
          ? Math.floor(update.index)
          : undefined,
      plan_version:
        typeof update.plan_version === "number" && Number.isFinite(update.plan_version)
          ? Math.floor(update.plan_version)
          : fallbackPlanVersion,
    });
  }
  return updates;
}

function applyTodoUpdates(
  todos: TodoItem[],
  updates: Array<{ id: string; status: TodoItem["status"]; text?: string; index?: number }>,
): TodoItem[] {
  if (!todos.length || !updates.length) {
    return todos;
  }

  const now = new Date().toISOString();
  const updatedTodos = todos.map((todo) => ({ ...todo }));

  const applyLegacyFallback = (status: TodoItem["status"]): void => {
    const activeIndex = updatedTodos.findIndex((todo) => todo.status === "in_progress");
    const firstPendingIndex = updatedTodos.findIndex((todo) => todo.status === "pending");
    const targetIndex = activeIndex !== -1 ? activeIndex : firstPendingIndex;
    if (targetIndex === -1) {
      return;
    }

    if (status === "completed") {
      for (let i = 0; i <= targetIndex; i += 1) {
        const todo = updatedTodos[i];
        if (todo.status !== "completed") {
          updatedTodos[i] = { ...todo, status: "completed", completedAt: now };
        }
      }
      const nextPendingIndex = updatedTodos.findIndex((todo) => todo.status === "pending");
      if (nextPendingIndex !== -1) {
        updatedTodos[nextPendingIndex] = { ...updatedTodos[nextPendingIndex], status: "in_progress" };
      }
      return;
    }

    if (status === "skipped") {
      updatedTodos[targetIndex] = { ...updatedTodos[targetIndex], status: "skipped", completedAt: now };
      return;
    }

    updatedTodos[targetIndex] = { ...updatedTodos[targetIndex], status };
  };

  updates.forEach((update) => {
    const hasIdentifier =
      (typeof update.id === "string" && update.id.trim().length > 0) ||
      typeof update.index === "number" ||
      (typeof update.text === "string" && update.text.trim().length > 0);
    let targetIndex = typeof update.index === "number" ? update.index : -1;
    if (targetIndex < 0 || targetIndex >= updatedTodos.length) {
      targetIndex = updatedTodos.findIndex((todo) => todo.id === update.id);
    }
    if (targetIndex === -1 && update.text) {
      const targetText = normalizeTodoText(update.text);
      targetIndex = updatedTodos.findIndex(
        (todo) => normalizeTodoText(todo.text) === targetText,
      );
    }
    if (targetIndex === -1) {
      if (!hasIdentifier) {
        applyLegacyFallback(update.status);
      }
      return;
    }

    if (update.status === "completed") {
      updatedTodos[targetIndex] = {
        ...updatedTodos[targetIndex],
        status: "completed",
        completedAt: now,
      };
      return;
    }

    if (update.status === "skipped") {
      updatedTodos[targetIndex] = {
        ...updatedTodos[targetIndex],
        status: "skipped",
        completedAt: now,
      };
      return;
    }

    if (update.status === "in_progress") {
      updatedTodos[targetIndex] = {
        ...updatedTodos[targetIndex],
        status: "in_progress",
      };
      return;
    }

    updatedTodos[targetIndex] = {
      ...updatedTodos[targetIndex],
      status: update.status,
    };
  });

  return updatedTodos;
}

function deriveRunStatusFromTodos(
  currentStatus: PlanRun["status"],
  todos: TodoItem[],
): PlanRun["status"] {
  if (currentStatus === "rejected" || currentStatus === "completed") {
    return currentStatus;
  }

  if (todos.length === 0) {
    return currentStatus;
  }

  const hasActive = todos.some((todo) => todo.status === "in_progress");
  const hasPending = todos.some((todo) => todo.status === "pending");
  if (!hasActive && !hasPending) {
    return "completed";
  }

  return "executing";
}

function planReducer(state: PlanState, action: PlanAction): PlanState {
  switch (action.type) {
    case "SET_PLAN": {
      const { payload, taskId } = action;
      const todoList = payload.todo_list.length === payload.plan_steps.length
        ? payload.todo_list
        : payload.plan_steps.map((text, index) => ({
            id: payload.todo_list[index]?.id ?? `${index + 1}`,
            text,
            status: payload.todo_list[index]?.status ?? "pending",
          }));
      const newRun: PlanRun = {
        runId: payload.run_id ?? Date.now(),
        timestamp: new Date().toISOString(),
        goal: payload.goal,
        planSteps: payload.plan_steps,
        todoList,
        planVersion: payload.plan_version ?? 1,
        status: "interrupted",
        reasoning: payload.reasoning,
      };

      return updateTaskState(state, taskId, (taskState) => {
        const history = taskState.currentRun
          ? [...taskState.runHistory, taskState.currentRun]
          : taskState.runHistory;
        return {
          currentRun: newRun,
          runHistory: history.slice(-9),
        };
      });
    }
    case "UPDATE_PLAN": {
      return updateTaskState(state, action.taskId, (taskState) => {
        if (!taskState.currentRun) return taskState;
        const nextPlanSteps = action.planSteps ?? taskState.currentRun.planSteps;
        const updatedTodoList = action.planSteps
          ? nextPlanSteps.map((text, index) => ({
              id: `${index + 1}`,
              text,
              status: "pending" as const,
            }))
          : taskState.currentRun.todoList;
        const nextPlanVersion =
          action.planVersion ??
          (action.planSteps ? taskState.currentRun.planVersion + 1 : taskState.currentRun.planVersion);
        return {
          ...taskState,
          currentRun: {
            ...taskState.currentRun,
            goal: action.goal ?? taskState.currentRun.goal,
            planSteps: nextPlanSteps,
            todoList: updatedTodoList,
            planVersion: nextPlanVersion,
          },
        };
      });
    }
    case "UPDATE_TODO": {
      return updateTaskState(state, action.taskId, (taskState) => {
        if (!taskState.currentRun) return taskState;
        const updatedTodos = applyTodoUpdates(taskState.currentRun.todoList, [
          { id: action.todoId, status: action.status },
        ]);
        const nextStatus = deriveRunStatusFromTodos(taskState.currentRun.status, updatedTodos);
        return {
          ...taskState,
          currentRun: { ...taskState.currentRun, todoList: updatedTodos, status: nextStatus },
        };
      });
    }
    case "APPLY_TODO_UPDATES": {
      return updateTaskState(state, action.taskId, (taskState) => {
        if (!taskState.currentRun) return taskState;
        const updatePlanVersion = action.updates.find((update) => update.plan_version !== undefined)?.plan_version;
        if (updatePlanVersion !== undefined && updatePlanVersion !== taskState.currentRun.planVersion) {
          return taskState;
        }
        const updatedTodos = applyTodoUpdates(taskState.currentRun.todoList, action.updates);
        const nextStatus = deriveRunStatusFromTodos(taskState.currentRun.status, updatedTodos);
        return {
          ...taskState,
          currentRun: { ...taskState.currentRun, todoList: updatedTodos, status: nextStatus },
        };
      });
    }
    case "INGEST_STREAM_PLAN_CREATED": {
      const { taskId, goal, planSteps, todoList, runId, planVersion } = action.payload;
      return updateTaskState(
        state,
        taskId,
        (taskState) => {
          const currentRun = taskState.currentRun;
          const resolvedRunId = typeof runId === "number" ? runId : currentRun?.runId ?? Date.now();
          const resolvedPlanVersion = typeof planVersion === "number" ? planVersion : currentRun?.planVersion ?? 1;

          if (
            currentRun &&
            resolvedRunId === currentRun.runId &&
            resolvedPlanVersion < currentRun.planVersion
          ) {
            return taskState;
          }

          const nextStatus = deriveRunStatusFromTodos("executing", todoList);
          const nextRun: PlanRun = {
            runId: resolvedRunId,
            timestamp: new Date().toISOString(),
            goal,
            planSteps,
            todoList,
            planVersion: resolvedPlanVersion,
            status: nextStatus,
            reasoning: currentRun?.reasoning,
          };

          if (!currentRun) {
            return {
              ...taskState,
              currentRun: nextRun,
            };
          }

          if (currentRun.runId === resolvedRunId) {
            return {
              ...taskState,
              currentRun: nextRun,
            };
          }

          return {
            currentRun: nextRun,
            runHistory: [...taskState.runHistory, currentRun].slice(-9),
          };
        },
        { setActiveTask: false },
      );
    }
    case "INGEST_STREAM_TODO_PROGRESS": {
      const { taskId, updates, runId, planVersion } = action.payload;
      return updateTaskState(
        state,
        taskId,
        (taskState) => {
          if (!taskState.currentRun) return taskState;
          if (typeof runId === "number" && runId !== taskState.currentRun.runId) {
            return taskState;
          }
          if (
            typeof planVersion === "number" &&
            planVersion !== taskState.currentRun.planVersion
          ) {
            return taskState;
          }
          const updatePlanVersion = updates.find((update) => update.plan_version !== undefined)?.plan_version;
          if (
            typeof updatePlanVersion === "number" &&
            updatePlanVersion !== taskState.currentRun.planVersion
          ) {
            return taskState;
          }
          const updatedTodos = applyTodoUpdates(taskState.currentRun.todoList, updates);
          const nextStatus = deriveRunStatusFromTodos(taskState.currentRun.status, updatedTodos);
          return {
            ...taskState,
            currentRun: { ...taskState.currentRun, todoList: updatedTodos, status: nextStatus },
          };
        },
        { setActiveTask: false },
      );
    }
    case "START_NEW_RUN": {
      return updateTaskState(state, action.taskId, (taskState) => {
        const history = taskState.currentRun
          ? [...taskState.runHistory, taskState.currentRun]
          : taskState.runHistory;
        return {
          currentRun: {
            runId: action.runId,
            timestamp: new Date().toISOString(),
            goal: "",
            planSteps: [],
            todoList: [],
            planVersion: 1,
            status: "planning",
          },
          runHistory: history.slice(-9),
        };
      });
    }
    case "COMPLETE_RUN":
      return updateTaskState(state, action.taskId, (taskState) => {
        if (!taskState.currentRun) return taskState;
        return {
          ...taskState,
          currentRun: { ...taskState.currentRun, status: "completed" },
        };
      });
    case "REJECT_RUN":
      return updateTaskState(state, action.taskId, (taskState) => {
        if (!taskState.currentRun) return taskState;
        const rejectedRun: PlanRun = { ...taskState.currentRun, status: "rejected" };
        return {
          currentRun: null,
          runHistory: [...taskState.runHistory, rejectedRun].slice(-9),
        };
      });
    case "SET_ACTIVE_TASK":
      if (state.activeTaskId === action.taskId) {
        return state;
      }
      return {
        ...state,
        activeTaskId: action.taskId,
      };
    case "SET_PLAN_CARD_MINIMIZED":
      return {
        ...state,
        uiByTask: {
          ...state.uiByTask,
          [action.taskId]: {
            ...getTaskUiState(state, action.taskId),
            isPlanCardMinimized: action.minimized,
          },
        },
      };
    case "SET_TODO_CARD_MINIMIZED":
      return {
        ...state,
        uiByTask: {
          ...state.uiByTask,
          [action.taskId]: {
            ...getTaskUiState(state, action.taskId),
            isTodoCardMinimized: action.minimized,
          },
        },
      };
    case "CLEAR_STATE": {
      if (action.taskId == null) {
        return initialState;
      }
      const { [action.taskId]: _, ...restRuns } = state.runsByTask;
      const { [action.taskId]: __, ...restUi } = state.uiByTask;
      return {
        ...state,
        runsByTask: restRuns,
        uiByTask: restUi,
        activeTaskId:
          state.activeTaskId === action.taskId ? null : state.activeTaskId,
      };
    }
    default:
      return state;
  }
}

interface PlanContextValue {
  state: PlanState;
  setPlan: (taskId: number, payload: PlanReviewPayload) => void;
  updatePlan: (taskId: number, goal?: string, planSteps?: string[], planVersion?: number) => void;
  updateTodo: (taskId: number, todoId: string, status: TodoItem["status"]) => void;
  applyTodoUpdates: (taskId: number, updates: Array<{ id: string; status: TodoItem["status"]; text?: string; index?: number; plan_version?: number }>) => void;
  startNewRun: (taskId: number, runId: number) => void;
  completeRun: (taskId: number) => void;
  rejectRun: (taskId: number) => void;
  setActiveTask: (taskId: number | null) => void;
  setPlanCardMinimized: (taskId: number, minimized: boolean) => void;
  setTodoCardMinimized: (taskId: number, minimized: boolean) => void;
  clearState: (taskId?: number | null) => void;
  getTodoProgress: (taskId: number | null) => { completed: number; total: number; percent: number };
  hasActivePlan: (taskId: number | null) => boolean;
  getTaskState: (taskId: number | null) => TaskPlanState;
  getTaskUiState: (taskId: number | null) => TaskPlanUiState;
}

const PlanContext = createContext<PlanContextValue | null>(null);

interface PlanProviderProps {
  children: ReactNode;
}

export function PlanProvider({ children }: PlanProviderProps) {
  const [state, dispatch] = useReducer(planReducer, initialState);
  const lastStreamSequenceByTaskRef = useRef<Map<number, number>>(new Map());

  const setPlan = useCallback((taskId: number, payload: PlanReviewPayload) => {
    dispatch({ type: "SET_PLAN", taskId, payload });
  }, []);

  const updatePlan = useCallback((taskId: number, goal?: string, planSteps?: string[], planVersion?: number) => {
    dispatch({ type: "UPDATE_PLAN", taskId, goal, planSteps, planVersion });
  }, []);

  const updateTodo = useCallback(
    (taskId: number, todoId: string, status: TodoItem["status"]) => {
      dispatch({ type: "UPDATE_TODO", taskId, todoId, status });
    },
    [],
  );

  const applyTodoUpdates = useCallback(
    (
      taskId: number,
      updates: Array<{ id: string; status: TodoItem["status"]; text?: string; index?: number; plan_version?: number }>,
    ) => {
      if (updates.length === 0) return;
      dispatch({ type: "APPLY_TODO_UPDATES", taskId, updates });
    },
    [],
  );

  const startNewRun = useCallback((taskId: number, runId: number) => {
    dispatch({ type: "START_NEW_RUN", taskId, runId });
  }, []);

  const completeRun = useCallback((taskId: number) => {
    dispatch({ type: "COMPLETE_RUN", taskId });
  }, []);

  const rejectRun = useCallback((taskId: number) => {
    dispatch({ type: "REJECT_RUN", taskId });
  }, []);

  const setActiveTask = useCallback((taskId: number | null) => {
    dispatch({ type: "SET_ACTIVE_TASK", taskId });
  }, []);

  const setPlanCardMinimized = useCallback((taskId: number, minimized: boolean) => {
    if (!Number.isFinite(taskId) || taskId <= 0) return;
    dispatch({ type: "SET_PLAN_CARD_MINIMIZED", taskId, minimized });
  }, []);

  const setTodoCardMinimized = useCallback((taskId: number, minimized: boolean) => {
    if (!Number.isFinite(taskId) || taskId <= 0) return;
    dispatch({ type: "SET_TODO_CARD_MINIMIZED", taskId, minimized });
  }, []);

  const clearState = useCallback((taskId?: number | null) => {
    dispatch({ type: "CLEAR_STATE", taskId });
    if (taskId == null) {
      lastStreamSequenceByTaskRef.current.clear();
      return;
    }
    if (!Number.isFinite(taskId) || taskId <= 0) {
      return;
    }
    lastStreamSequenceByTaskRef.current.delete(Math.floor(taskId));
  }, []);

  const shouldProcessStreamSequence = useCallback((taskId: number, sequence?: number): boolean => {
    if (typeof sequence !== "number" || !Number.isFinite(sequence)) {
      return true;
    }
    const normalizedSequence = Math.floor(sequence);
    if (normalizedSequence < 0) {
      return false;
    }
    const previous = lastStreamSequenceByTaskRef.current.get(taskId);
    if (typeof previous === "number" && normalizedSequence <= previous) {
      return false;
    }
    lastStreamSequenceByTaskRef.current.set(taskId, normalizedSequence);
    return true;
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const onPlanCreated = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (!detail || typeof detail !== "object") {
        return;
      }
      const payload = detail as Record<string, unknown>;
      const taskId = Number(payload.taskId);
      if (!Number.isFinite(taskId) || taskId <= 0) {
        return;
      }
      const normalizedTaskId = Math.floor(taskId);
      const sequence = typeof payload.sequence === "number" ? payload.sequence : undefined;
      if (!shouldProcessStreamSequence(normalizedTaskId, sequence)) {
        return;
      }

      const rawPlanSteps = Array.isArray(payload.planSteps) ? payload.planSteps : [];
      const planSteps = normalizePlanSteps(rawPlanSteps);
      const rawTodoList = Array.isArray(payload.todoList) ? payload.todoList : [];
      const todoList = normalizeTodoList(rawTodoList, planSteps);
      if (planSteps.length === 0 && todoList.length === 0) {
        return;
      }

      dispatch({
        type: "INGEST_STREAM_PLAN_CREATED",
        payload: {
          taskId: normalizedTaskId,
          goal: typeof payload.goal === "string" ? payload.goal : "",
          planSteps,
          todoList,
          runId:
            typeof payload.runId === "number" && Number.isFinite(payload.runId)
              ? Math.floor(payload.runId)
              : undefined,
          planVersion:
            typeof payload.planVersion === "number" && Number.isFinite(payload.planVersion)
              ? Math.floor(payload.planVersion)
              : undefined,
          sequence,
        },
      });
    };

    const onTodoProgress = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (!detail || typeof detail !== "object") {
        return;
      }
      const payload = detail as Record<string, unknown>;
      const taskId = Number(payload.taskId);
      if (!Number.isFinite(taskId) || taskId <= 0) {
        return;
      }
      const normalizedTaskId = Math.floor(taskId);
      const sequence = typeof payload.sequence === "number" ? payload.sequence : undefined;
      if (!shouldProcessStreamSequence(normalizedTaskId, sequence)) {
        return;
      }
      const planVersion =
        typeof payload.planVersion === "number" && Number.isFinite(payload.planVersion)
          ? Math.floor(payload.planVersion)
          : undefined;
      const rawUpdates = Array.isArray(payload.updates) ? payload.updates : [];
      const updates = normalizeTodoUpdates(rawUpdates, planVersion);
      if (updates.length === 0) {
        return;
      }
      dispatch({
        type: "INGEST_STREAM_TODO_PROGRESS",
        payload: {
          taskId: normalizedTaskId,
          updates,
          runId:
            typeof payload.runId === "number" && Number.isFinite(payload.runId)
              ? Math.floor(payload.runId)
              : undefined,
          planVersion,
          sequence,
        },
      });
    };

    window.addEventListener("task-plan-created", onPlanCreated as EventListener);
    window.addEventListener("task-todo-progress", onTodoProgress as EventListener);
    return () => {
      window.removeEventListener("task-plan-created", onPlanCreated as EventListener);
      window.removeEventListener("task-todo-progress", onTodoProgress as EventListener);
    };
  }, [shouldProcessStreamSequence]);

  const getTodoProgress = useCallback(
    (taskId: number | null) => {
      if (taskId == null) {
        return { completed: 0, total: 0, percent: 0 };
      }
      const todos = getTaskState(state, taskId).currentRun?.todoList ?? [];
      const total = todos.length;
      const completed = todos.filter((t) => t.status === "completed").length;
      const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
      return { completed, total, percent };
    },
    [state],
  );

  const hasActivePlan = useCallback(
    (taskId: number | null) =>
      taskId != null &&
      getTaskState(state, taskId).currentRun !== null &&
      getTaskState(state, taskId).currentRun?.planSteps.length !== 0,
    [state],
  );

  const getTaskStateForTask = useCallback(
    (taskId: number | null) => {
      if (taskId == null) {
        return { currentRun: null, runHistory: [] };
      }
      return getTaskState(state, taskId);
    },
    [state],
  );

  const getTaskUiStateForTask = useCallback(
    (taskId: number | null) => {
      if (taskId == null) {
        return { isPlanCardMinimized: false, isTodoCardMinimized: false };
      }
      return getTaskUiState(state, taskId);
    },
    [state],
  );

  const value = useMemo(
    () => ({
      state,
      setPlan,
      updatePlan,
      updateTodo,
      applyTodoUpdates,
      startNewRun,
      completeRun,
      rejectRun,
      setActiveTask,
      setPlanCardMinimized,
      setTodoCardMinimized,
      clearState,
      getTodoProgress,
      hasActivePlan,
      getTaskState: getTaskStateForTask,
      getTaskUiState: getTaskUiStateForTask,
    }),
    [
      state,
      setPlan,
      updatePlan,
      updateTodo,
      applyTodoUpdates,
      startNewRun,
      completeRun,
      rejectRun,
      setActiveTask,
      setPlanCardMinimized,
      setTodoCardMinimized,
      clearState,
      getTodoProgress,
      hasActivePlan,
      getTaskStateForTask,
      getTaskUiStateForTask,
    ],
  );

  return <PlanContext.Provider value={value}>{children}</PlanContext.Provider>;
}

export function usePlanContext(): PlanContextValue {
  const context = useContext(PlanContext);
  if (!context) {
    throw new Error("usePlanContext must be used within PlanProvider");
  }
  return context;
}

export function usePlanTaskSync(taskId: number | null): void {
  const { setActiveTask } = usePlanContext();
  const prevTaskId = useRef(taskId);

  useEffect(() => {
    if (prevTaskId.current !== taskId) {
      setActiveTask(taskId ?? null);
      prevTaskId.current = taskId;
    }
  }, [taskId, setActiveTask]);
}

export default PlanContext;
