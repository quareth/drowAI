// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WorkspaceTabBar } from "@/components/workspace/workspace-tab-bar";

describe("WorkspaceTabBar", () => {
  it("renders tabs and calls back when a new tab is selected", () => {
    const handleTabChange = vi.fn();

    render(
      <WorkspaceTabBar
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "files", label: "Files" },
        ]}
        activeTab="overview"
        onTabChange={handleTabChange}
      />,
    );

    const overviewButton = screen.getByRole("button", { name: "Overview" });
    const filesButton = screen.getByRole("button", { name: "Files" });

    expect(overviewButton.className).toContain("text-emerald-400");
    expect(filesButton.className).toContain("text-slate-400");

    fireEvent.click(filesButton);
    expect(handleTabChange).toHaveBeenCalledWith("files");
  });
});
