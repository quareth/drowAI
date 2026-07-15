// @vitest-environment jsdom
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FileExplorerPanel,
  type FileExplorerSelection,
} from "@/components/panels/file-explorer-panel";

const mockApiFetch = vi.fn();
const mockToast = vi.fn();

Object.assign(HTMLElement.prototype, {
  hasPointerCapture: HTMLElement.prototype.hasPointerCapture ?? (() => false),
  setPointerCapture: HTMLElement.prototype.setPointerCapture ?? (() => undefined),
  releasePointerCapture: HTMLElement.prototype.releasePointerCapture ?? (() => undefined),
});
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => undefined);

vi.mock("@/lib/api-config", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

interface MockTreeNode {
  name: string;
  type: "file" | "folder";
  path: string;
  size: number | null;
  modified: string;
  content_availability?: string;
  children: MockTreeNode[];
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function SelectionProbe({ selection }: { selection: FileExplorerSelection }) {
  return (
    <div
      data-testid="selection"
      data-task-id={selection.taskId ?? ""}
      data-file-path={selection.filePath ?? ""}
    />
  );
}

function renderPanel(initialSelection: Partial<FileExplorerSelection> = {}) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  function Harness() {
    const [selection, setSelection] = useState<FileExplorerSelection>({
      taskId: initialSelection.taskId ?? null,
      filePath: initialSelection.filePath ?? null,
    });

    return (
      <>
        <SelectionProbe selection={selection} />
        <FileExplorerPanel
          selectedTaskId={selection.taskId}
          selectedFile={selection.filePath}
          onSelectionChange={setSelection}
        />
      </>
    );
  }

  return render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

const tasksPayload = [
  {
    id: 1,
    user_id: 1,
    name: "Running Task",
    status: "running",
    created_at: "2026-02-08T12:00:00Z",
    updated_at: "2026-02-08T12:00:00Z",
  },
  {
    id: 2,
    user_id: 1,
    name: "Completed Task",
    status: "completed",
    created_at: "2026-02-07T12:00:00Z",
    updated_at: "2026-02-07T12:00:00Z",
  },
];

const baseTreeTask1: MockTreeNode = {
  name: "workspace",
  type: "folder",
  path: "/",
  size: null,
  modified: "2026-02-08T12:00:00Z",
  children: [
    {
      name: "docs",
      type: "folder",
      path: "/docs",
      size: null,
      modified: "2026-02-08T12:00:00Z",
      children: [
        {
          name: "archive",
          type: "folder",
          path: "/docs/archive",
          size: null,
          modified: "2026-02-08T12:00:00Z",
          children: [
            {
              name: "report.md",
              type: "file",
              path: "/docs/archive/report.md",
              size: 1200,
              modified: "2026-02-08T12:00:00Z",
              children: [],
            },
          ],
        },
      ],
    },
    {
      name: "scans",
      type: "folder",
      path: "/scans",
      size: null,
      modified: "2026-02-08T12:00:00Z",
      children: [
        {
          name: "nmap.xml",
          type: "file",
          path: "/scans/nmap.xml",
          size: 2048,
          modified: "2026-02-08T12:00:00Z",
          children: [],
        },
      ],
    },
  ],
};

const treeTask2: MockTreeNode = {
  name: "workspace",
  type: "folder",
  path: "/",
  size: null,
  modified: "2026-02-07T12:00:00Z",
  children: [
    {
      name: "logs",
      type: "folder",
      path: "/logs",
      size: null,
      modified: "2026-02-07T12:00:00Z",
      children: [
        {
          name: "agent.log",
          type: "file",
          path: "/logs/agent.log",
          size: 256,
          modified: "2026-02-07T12:00:00Z",
          children: [],
        },
      ],
    },
  ],
};

const searchPayload = {
  query: "report",
  results: [
    {
      name: "report.md",
      type: "file",
      path: "/docs/archive/report.md",
      size: 1200,
      modified: "2026-02-08T12:00:00Z",
    },
  ],
  total_count: 1,
  truncated: false,
};

function setupApiMocks() {
  let treeFetchCountTask1 = 0;

  mockApiFetch.mockImplementation(async (endpoint: unknown, options?: RequestInit) => {
    const url = String(endpoint);

    if (url === "/api/tasks/") {
      return jsonResponse(tasksPayload);
    }

    if (url.includes("/api/tasks/1/files/tree")) {
      treeFetchCountTask1 += 1;
      return jsonResponse(baseTreeTask1);
    }

    if (url.includes("/api/tasks/2/files/tree")) {
      return jsonResponse(treeTask2);
    }

    if (url.includes("/api/tasks/1/files/search?q=report")) {
      return jsonResponse(searchPayload);
    }

    if (url.includes("/api/tasks/1/files/download-multiple") && options?.method === "POST") {
      return new Response("zip-content", {
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": "attachment; filename=workspace-files.zip",
        },
      });
    }

    return jsonResponse({ detail: `Unhandled endpoint: ${url}` }, 404);
  });

  return {
    getTreeFetchCountTask1: () => treeFetchCountTask1,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockToast.mockReset();
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock-url");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FileExplorerPanel interactions", () => {
  it("selects task locally and toggles tree folders", async () => {
    setupApiMocks();
    renderPanel();

    const taskSelect = await screen.findByLabelText("Select task");
    fireEvent.pointerDown(taskSelect, {
      button: 0,
      ctrlKey: false,
      pointerId: 1,
      pointerType: "mouse",
    });
    fireEvent.click(await screen.findByRole("option", { name: "Running Task (#1)" }));

    await waitFor(() => {
      expect(screen.getByTestId("selection").getAttribute("data-task-id")).toBe("1");
    });
    expect(window.location.search).toBe("");

    expect(await screen.findByText("docs")).toBeTruthy();

    expect(screen.queryByText("archive")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "docs" }));
    expect(await screen.findByText("archive")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "docs" }));
    await waitFor(() => {
      expect(screen.queryByText("archive")).toBeNull();
    });
  });

  it("searches files and clears search when file is clicked", async () => {
    setupApiMocks();
    renderPanel({ taskId: 1 });

    expect(await screen.findByText("docs")).toBeTruthy();

    const searchInput = screen.getByPlaceholderText("Search files...") as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "report" } });

    await waitFor(() => {
      const called = mockApiFetch.mock.calls.some(([url]) =>
        String(url).includes("/api/tasks/1/files/search?q=report"),
      );
      expect(called).toBe(true);
    });

    const reportNode = await screen.findByText("report.md");
    fireEvent.click(reportNode);

    await waitFor(() => {
      expect(searchInput.value).toBe("");
    });

    await waitFor(() => {
      const selection = screen.getByTestId("selection");
      expect(selection.getAttribute("data-task-id")).toBe("1");
      expect(selection.getAttribute("data-file-path")).toBe("/docs/archive/report.md");
    });
    expect(window.location.search).toBe("");
  });

  it("supports breadcrumb navigation and refresh preserves expanded + selected state", async () => {
    const stats = setupApiMocks();
    renderPanel({ taskId: 1 });

    expect(await screen.findByText("docs")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "docs" }));
    expect(await screen.findByText("archive")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "archive" }));
    const reportNode = await screen.findByText("report.md");
    fireEvent.click(reportNode);

    await waitFor(() => {
      expect(screen.getByTestId("selection").getAttribute("data-file-path")).toBe("/docs/archive/report.md");
    });

    fireEvent.click(screen.getByLabelText("Refresh files"));

    await waitFor(() => {
      expect(stats.getTreeFetchCountTask1()).toBeGreaterThanOrEqual(2);
    });

    expect(reportNode.closest("button")?.className).toContain("bg-slate-700");

    const breadcrumb = screen.getByTestId("file-breadcrumb");
    fireEvent.click(within(breadcrumb).getByRole("button", { name: "docs" }));

    await waitFor(() => {
      const selection = screen.getByTestId("selection");
      expect(selection.getAttribute("data-task-id")).toBe("1");
      expect(selection.getAttribute("data-file-path")).toBe("");
    });

    expect(screen.getByText("archive")).toBeTruthy();
  });

  it("supports ctrl/cmd multi-select and downloads selected files as zip", async () => {
    setupApiMocks();
    renderPanel({ taskId: 1 });

    expect(await screen.findByText("docs")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "docs" }));
    fireEvent.click(await screen.findByRole("button", { name: "archive" }));
    fireEvent.click(screen.getByRole("button", { name: "scans" }));

    fireEvent.click((await screen.findByText("report.md")).closest("button") as HTMLButtonElement, {
      ctrlKey: true,
    });
    fireEvent.click((await screen.findByText("nmap.xml")).closest("button") as HTMLButtonElement, {
      metaKey: true,
    });

    const downloadSelectedButton = await screen.findByRole("button", { name: "Download selected files" });
    expect(downloadSelectedButton.textContent).toContain("Download Selected (2)");
    fireEvent.click(downloadSelectedButton);

    expect(await screen.findByText("Download selected files")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Download as ZIP" }));

    await waitFor(() => {
      const zipCall = mockApiFetch.mock.calls.find(([url]) =>
        String(url).includes("/api/tasks/1/files/download-multiple"),
      );
      expect(zipCall).toBeTruthy();
    });

    const zipCall = mockApiFetch.mock.calls.find(([url]) =>
      String(url).includes("/api/tasks/1/files/download-multiple"),
    );
    const requestBody = JSON.parse(String(zipCall?.[1]?.body ?? "{}"));
    expect(requestBody.paths).toContain("/docs/archive/report.md");
    expect(requestBody.paths).toContain("/scans/nmap.xml");

    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /Download Selected/ })).toBeNull();
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Downloading workspace-files.zip",
      }),
    );
  });

  it("closes download dialog on cancel without calling zip endpoint", async () => {
    setupApiMocks();
    renderPanel({ taskId: 1 });

    expect(await screen.findByText("docs")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "docs" }));
    fireEvent.click(screen.getByRole("button", { name: "archive" }));
    fireEvent.click((await screen.findByText("report.md")).closest("button") as HTMLButtonElement, {
      ctrlKey: true,
    });

    const downloadSelectedButton = await screen.findByRole("button", { name: "Download selected files" });
    expect(downloadSelectedButton.textContent).toContain("Download Selected (1)");
    fireEvent.click(downloadSelectedButton);
    expect(await screen.findByText("Download selected files")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByText("Download selected files")).toBeNull();
    });

    const zipCalls = mockApiFetch.mock.calls.filter(([url]) =>
      String(url).includes("/files/download-multiple"),
    );
    expect(zipCalls).toHaveLength(0);
  });

  it("renders pending/failed upload states without selecting unavailable files", async () => {
    const treeWithUploadStates: MockTreeNode = {
      name: "workspace",
      type: "folder",
      path: "/",
      size: null,
      modified: "2026-02-08T12:00:00Z",
      children: [
        {
          name: "uploads",
          type: "folder",
          path: "/uploads",
          size: null,
          modified: "2026-02-08T12:00:00Z",
          children: [
            {
              name: "pending.txt",
              type: "file",
              path: "/uploads/pending.txt",
              size: 128,
              modified: "2026-02-08T12:00:00Z",
              content_availability: "upload_pending",
              children: [],
            },
            {
              name: "failed.txt",
              type: "file",
              path: "/uploads/failed.txt",
              size: 256,
              modified: "2026-02-08T12:00:00Z",
              content_availability: "upload_failed",
              children: [],
            },
            {
              name: "ready.txt",
              type: "file",
              path: "/uploads/ready.txt",
              size: 512,
              modified: "2026-02-08T12:00:00Z",
              content_availability: "available_object",
              children: [],
            },
          ],
        },
      ],
    };

    mockApiFetch.mockImplementation(async (endpoint: unknown) => {
      const url = String(endpoint);
      if (url === "/api/tasks/") {
        return jsonResponse(tasksPayload);
      }
      if (url.includes("/api/tasks/1/files/tree")) {
        return jsonResponse(treeWithUploadStates);
      }
      return jsonResponse({ detail: `Unhandled endpoint: ${url}` }, 404);
    });

    renderPanel({ taskId: 1 });

    expect(await screen.findByText("uploads")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "uploads" }));

    expect(await screen.findByText("Uploading")).toBeTruthy();
    expect(await screen.findByText("Upload failed")).toBeTruthy();

    fireEvent.click((await screen.findByText("pending.txt")).closest("button") as HTMLButtonElement);
    fireEvent.click((await screen.findByText("failed.txt")).closest("button") as HTMLButtonElement);

    await waitFor(() => {
      const selection = screen.getByTestId("selection");
      expect(selection.getAttribute("data-task-id")).toBe("1");
      expect(selection.getAttribute("data-file-path")).toBe("");
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Upload in progress" }),
    );
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Upload failed" }),
    );

    fireEvent.click((await screen.findByText("ready.txt")).closest("button") as HTMLButtonElement);
    await waitFor(() => {
      expect(screen.getByTestId("selection").getAttribute("data-file-path")).toBe("/uploads/ready.txt");
    });
    expect(window.location.search).toBe("");
  });
});
