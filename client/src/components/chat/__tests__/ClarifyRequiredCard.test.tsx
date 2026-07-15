// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ClarifyRequiredCard from "@/components/chat/ClarifyRequiredCard";
import type { ClarifyRequestPayload } from "@/types/hitl";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("ClarifyRequiredCard", () => {
  it("renders select-only questions and no text input", () => {
    const onSubmit = vi.fn();
    const payload: ClarifyRequestPayload = {
      type: "clarify_request",
      questions: [
        {
          question_id: "target",
          input_type: "select",
          label: "Which host should be scanned?",
          options: ["10.0.0.1", "10.0.0.2"],
          required: true,
        },
      ],
    };

    render(<ClarifyRequiredCard payload={payload} onSubmit={onSubmit} />);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByLabelText("Which host should be scanned?")).toBeTruthy();
  });

  it("renders select options and submits selected answers map", () => {
    const onSubmit = vi.fn();
    const payload: ClarifyRequestPayload = {
      type: "clarify_request",
      questions: [
        {
          question_id: "target",
          input_type: "select",
          label: "Which host should be scanned?",
          options: ["10.0.0.1", "10.0.0.2"],
          required: true,
        },
        {
          question_id: "scan_mode",
          input_type: "select",
          label: "Which scan mode?",
          options: ["quick", "full"],
          required: true,
        },
      ],
    };

    render(<ClarifyRequiredCard payload={payload} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText("Which host should be scanned?"), {
      target: { value: "10.0.0.2" },
    });
    fireEvent.change(screen.getByLabelText("Which scan mode?"), {
      target: { value: "full" },
    });
    fireEvent.click(screen.getByText("Submit Answers"));

    expect(onSubmit).toHaveBeenCalledWith(
      {
        target: "10.0.0.2",
        scan_mode: "full",
      },
    );
  });

  it("does not submit on option change; submits only on click", () => {
    const onSubmit = vi.fn();
    const payload: ClarifyRequestPayload = {
      type: "clarify_request",
      questions: [
        {
          question_id: "scan_mode",
          input_type: "select",
          label: "Which scan mode?",
          options: ["quick", "full"],
          required: true,
        },
      ],
    };

    render(<ClarifyRequiredCard payload={payload} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText("Which scan mode?"), {
      target: { value: "full" },
    });
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Submit Answers"));
    expect(onSubmit).toHaveBeenCalledWith({ scan_mode: "full" });
  });

  it("does not submit when required selection is missing", () => {
    const onSubmit = vi.fn();
    const payload: ClarifyRequestPayload = {
      type: "clarify_request",
      questions: [
        {
          question_id: "target",
          input_type: "select",
          label: "Which host should be scanned?",
          options: [],
          required: true,
        },
      ],
    };

    render(<ClarifyRequiredCard payload={payload} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText("Submit Answers"));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("This field is required.")).toBeTruthy();
  });
});
