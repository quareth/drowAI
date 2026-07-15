// @vitest-environment jsdom
/**
 * Tests that phase cards remain non-expandable without content and become
 * interactive once content exists.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ObservingCard } from "@/components/chat/ObservingCard";
import { ThinkingCard } from "@/components/chat/ThinkingCard";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("ThinkingCard", () => {
  it("is not expandable when content is missing", () => {
    render(<ThinkingCard steps={[]} />);

    const header = screen.getByRole("button");
    expect(header.getAttribute("disabled")).toBe("");
    expect(screen.queryByText("analysis")).toBeNull();
  });

  it("becomes expandable when content exists", () => {
    render(<ThinkingCard steps={["analysis"]} />);

    const header = screen.getAllByRole("button")[0];
    expect(header.getAttribute("disabled")).toBeNull();
    fireEvent.click(header);
    expect(screen.getByText("analysis")).toBeTruthy();
  });

  it("renders completed duration when available", () => {
    render(
      <ThinkingCard
        steps={["analysis"]}
        isInProgress={false}
        durationMs={400}
      />,
    );

    expect(screen.getByText("Thought for 0.4s")).toBeTruthy();
  });

  it("falls back to Thought when duration is unavailable", () => {
    render(<ThinkingCard steps={["analysis"]} isInProgress={false} />);

    expect(screen.getByText("Thought")).toBeTruthy();
  });

  it("renders reasoning sections as an accessible vertical timeline", () => {
    const steps = [
      "Analyzing the request.",
      "Selecting relevant tool categories.",
      "Preparing tool execution.",
    ];
    render(<ThinkingCard steps={steps} isInProgress />);

    expect(screen.getByText("Preparing tool execution.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));

    const timeline = screen.getByRole("list", { name: "Thinking steps" });
    const items = within(timeline).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[2].getAttribute("aria-current")).toBe("step");
    expect(within(timeline).getByText("Selecting relevant tool categories.")).toBeTruthy();
  });

  it("summarizes the number of completed reasoning steps", () => {
    render(<ThinkingCard steps={["one", "two", "three"]} durationMs={400} />);

    expect(screen.getByText("Thought for 0.4s")).toBeTruthy();
    expect(screen.getByText("3 steps")).toBeTruthy();
  });
});

describe("ObservingCard", () => {
  it("is not expandable when content is missing", () => {
    render(<ObservingCard observation="observation" hasContent={false} />);

    const header = screen.getByRole("button");
    expect(header.getAttribute("disabled")).toBe("");
    expect(screen.queryByText("observation")).toBeNull();
  });

  it("becomes expandable when content exists", () => {
    render(<ObservingCard observation="observation" hasContent={true} />);

    const header = screen.getAllByRole("button")[0];
    expect(header.getAttribute("disabled")).toBeNull();
    fireEvent.click(header);
    expect(screen.getByText("observation")).toBeTruthy();
  });
});
