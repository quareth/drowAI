/**
 * Verifies the compact task-panel toolbar keeps its controls in the intended order.
 */
// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TaskPanelToolbar } from "@/components/panels/task-panel-toolbar";

afterEach(() => {
  cleanup();
});

describe("TaskPanelToolbar", () => {
  it("places the panel toggle before the view controls", () => {
    render(
      <TaskPanelToolbar
        viewMode="grouped"
        onViewMode={vi.fn()}
        onNewTask={vi.fn()}
        onNewEngagement={vi.fn()}
        nameFilter=""
        onNameFilterChange={vi.fn()}
        onToggleVisibility={vi.fn()}
      />,
    );

    const panelToggle = screen.getByRole("button", { name: "Close task panel" });
    const groupedView = screen.getByTitle("Grouped view");

    expect(
      panelToggle.compareDocumentPosition(groupedView)
      & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
