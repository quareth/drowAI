/**
 * Regression coverage for queued-message popover layout and interactions.
 */
// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import QueueIndicator from "@/components/chat/QueueIndicator";
import type { QueueItem, SendQueueApi } from "@/hooks/useSendQueue";

function queueItem(index: number): QueueItem {
  return {
    id: `queue-${index}`,
    content: `Queued prompt ${index}`,
    createdAt: `2026-07-28T12:00:0${index}.000Z`,
    conversationId: "conversation-1",
  };
}

function createQueue(count: number): SendQueueApi {
  return {
    items: Array.from({ length: count }, (_, index) => queueItem(index + 1)),
    count,
    onUserSend: vi.fn(async () => undefined),
    remove: vi.fn(),
    modify: vi.fn(),
    clear: vi.fn(),
  };
}

afterEach(() => {
  cleanup();
});

describe("QueueIndicator", () => {
  it("does not render an indicator for an empty queue", () => {
    render(<QueueIndicator queue={createQueue(0)} />);

    expect(screen.queryByRole("button", { name: /Queued/ })).toBeNull();
  });

  it("portals a viewport-constrained, independently scrollable queue card", async () => {
    const view = render(<QueueIndicator queue={createQueue(6)} />);
    const trigger = screen.getByRole("button", { name: "6 Queued" });

    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: "Queued" });
    const list = screen.getByRole("list", { name: "Queued messages" });

    expect(view.container.contains(dialog)).toBe(false);
    expect(dialog.className).toContain("max-h-[--radix-popover-content-available-height]");
    expect(dialog.className).toContain("overflow-hidden");
    expect(list.className).toContain("min-h-0");
    expect(list.className).toContain("max-h-72");
    expect(list.className).toContain("overflow-y-auto");
    expect(screen.getAllByRole("listitem")).toHaveLength(6);

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Queued" })).toBeNull();
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(trigger);
    });
  });

  it("dismisses outside and preserves modify, save, cancel, remove, and close controls", async () => {
    const queue = createQueue(5);
    render(<QueueIndicator queue={queue} />);
    const trigger = screen.getByRole("button", { name: "5 Queued" });

    fireEvent.click(trigger);
    await screen.findByRole("dialog", { name: "Queued" });
    fireEvent.pointerDown(document.body);
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Queued" })).toBeNull();
    });

    fireEvent.click(trigger);
    const items = await screen.findAllByRole("listitem");
    fireEvent.click(within(items[0]).getByRole("button", { name: /Modify/ }));

    const editor = screen.getByRole("textbox", { name: "Edit queued message" });
    fireEvent.change(editor, { target: { value: "Updated queued prompt" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(queue.modify).toHaveBeenCalledWith("queue-1", "Updated queued prompt");

    fireEvent.click(within(items[1]).getByRole("button", { name: /Modify/ }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(queue.modify).toHaveBeenCalledTimes(1);

    fireEvent.click(within(items[4]).getByRole("button", { name: /Remove/ }));
    expect(queue.remove).toHaveBeenCalledWith("queue-5");

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Queued" })).toBeNull();
    });
  });
});
