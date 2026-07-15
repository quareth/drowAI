// @vitest-environment jsdom
/* Tests for the compact reporting input status badge. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ReportingStatusBadge } from "@/components/reporting/reporting-status-badge";
import type { ReportingInputState } from "@/types/reporting";

afterEach(() => {
  cleanup();
});

describe("<ReportingStatusBadge />", () => {
  it("maps every reporting input state to its product label", () => {
    const states: Array<[ReportingInputState, string]> = [
      ["not_prepared", "Not prepared"],
      ["preparing", "Preparing"],
      ["failed", "Failed"],
      ["stale", "Stale"],
    ];

    for (const [inputState, label] of states) {
      const { unmount } = render(<ReportingStatusBadge inputState={inputState} />);

      const badge = screen.getByLabelText(`Reporting input status: ${label}`);
      expect(badge.textContent).toBe(label);
      expect(badge.className).toContain("h-5");
      expect(badge.className).toContain("whitespace-nowrap");

      unmount();
    }
  });

  it("does not render a badge for normal ready inputs", () => {
    const { container } = render(<ReportingStatusBadge inputState="ready" />);

    expect(container.firstChild).toBeNull();
  });

  it("uses a neutral unavailable badge for unknown input states", () => {
    render(
      <>
        <ReportingStatusBadge inputState="cancelled" />
        <ReportingStatusBadge inputState="__proto__" />
      </>,
    );

    const badges = screen.getAllByLabelText("Reporting input status: Unavailable");
    expect(badges).toHaveLength(2);

    const badge = badges[0];
    expect(badge.textContent).toBe("Unavailable");
    expect(badge.className).toContain("border-slate-700");
    expect(badge.className).toContain("text-slate-400");
  });
});
