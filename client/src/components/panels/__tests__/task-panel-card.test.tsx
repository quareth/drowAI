/**
 * TaskPanelCard compact reporting controls and legacy action behavior.
 */
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TaskPanelCard, type TaskPanelCardProps } from "@/components/panels/task-panel-card";
import type { Task } from "@/types";

vi.mock("@/components/docker-terminal", () => ({
  DockerTerminal: () => null,
}));

vi.mock("@/components/resources-panel", () => ({
  ResourcesPanel: () => null,
}));

const completedTask: Task = {
  id: 101,
  user_id: 1,
  engagement_id: 7,
  engagement_name: "Client Alpha",
  name: "Completed task",
  scope: "example scope",
  status: "completed",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function renderCard(
  task: Task,
  overrides: Partial<TaskPanelCardProps> = {},
) {
  const props: TaskPanelCardProps = {
    task,
    selectedTask: null,
    showContainerMonitor: false,
    taskActionPending: false,
    isRefreshing: false,
    isTaskDeleting: vi.fn(() => false),
    onToggleMonitor: vi.fn(),
    onRefresh: vi.fn(),
    onViewDetails: vi.fn(),
    onDelete: vi.fn(),
    onTaskAction: vi.fn(),
    onOpenTerminal: vi.fn(),
    ...overrides,
  };

  return render(<TaskPanelCard {...props} />);
}

describe("<TaskPanelCard /> reporting shortcuts", () => {
  afterEach(() => {
    cleanup();
  });

  it("keeps the completed-task Report button when reporting props are absent", () => {
    renderCard(completedTask);

    expect(screen.getByRole("button", { name: "Report" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Reports Workspace" })).toBeNull();
    expect(screen.queryByText("Report input:")).toBeNull();
  });

  it("replaces the completed-task Report button with workspace navigation", () => {
    const onOpenReportsWorkspace = vi.fn();

    renderCard(completedTask, { onOpenReportsWorkspace });

    expect(screen.queryByRole("button", { name: "Report" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reports Workspace" }));

    expect(onOpenReportsWorkspace).toHaveBeenCalledWith(7);
  });

  it("shows compact reporting state and keeps Prepare separate from runtime controls", () => {
    const onTaskAction = vi.fn();
    const onPrepareReportingInput = vi.fn();

    renderCard(
      {
        ...completedTask,
        status: "running",
        name: "Runtime task",
      },
      {
        reportingInputState: "ready",
        canPrepareReportingInput: true,
        onTaskAction,
        onPrepareReportingInput,
      },
    );

    expect(screen.getByText("Report input:")).toBeTruthy();
    expect(screen.getByText("Ready")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "View" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    fireEvent.click(screen.getByRole("button", { name: "Prepare" }));

    expect(onTaskAction).toHaveBeenCalledWith(101, "stop");
    expect(onPrepareReportingInput).toHaveBeenCalledWith(101);
  });

  it("does not render Prepare without a preparation callback", () => {
    renderCard(
      {
        ...completedTask,
        status: "running",
      },
      {
        reportingInputState: "stale",
        canPrepareReportingInput: true,
      },
    );

    expect(screen.queryByRole("button", { name: "Prepare" })).toBeNull();
  });

  it("disables Prepare while that task input is preparing", () => {
    const onPrepareReportingInput = vi.fn();

    renderCard(completedTask, {
      reportingInputState: "stale",
      canPrepareReportingInput: true,
      isPreparingReportingInput: true,
      onPrepareReportingInput,
    });

    const prepareButton = screen.getByRole("button", { name: "Preparing..." }) as HTMLButtonElement;
    expect(prepareButton.disabled).toBe(true);
    fireEvent.click(prepareButton);
    expect(onPrepareReportingInput).not.toHaveBeenCalled();
  });

  it("does not expose the placeholder Download Logs menu action", async () => {
    renderCard(completedTask);

    const taskActionsButton = screen.getByRole("button", {
      name: "Task actions for Completed task",
    });
    fireEvent.pointerDown(taskActionsButton, { button: 0, ctrlKey: false });

    expect(await screen.findByRole("menuitem", { name: "View Details" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Download Logs" })).toBeNull();
  });

  it("does not expose the development-only Memory Flow action", async () => {
    renderCard(completedTask);
    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Task actions for Completed task" }),
      { button: 0, ctrlKey: false },
    );
    expect(await screen.findByRole("menuitem", { name: "View Details" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Memory Flow" })).toBeNull();
  });

  it.each(["created", "stopped", "failed", "timeout"])(
    "offers Start for the domain restartable status %s",
    (status) => {
      const onTaskAction = vi.fn();
      renderCard({ ...completedTask, status, name: `${status} task` }, { onTaskAction });

      fireEvent.click(screen.getByRole("button", { name: "Start" }));

      expect(onTaskAction).toHaveBeenCalledWith(101, "start");
    },
  );
});
