// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FilePreviewPanel } from "@/components/panels/file-preview-panel";

const mockApiFetch = vi.fn();
const mockToast = vi.fn();

vi.mock("@/lib/api-config", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function createPreviewPayload(overrides: Record<string, unknown> = {}) {
  return {
    path: "/docs/report.md",
    name: "report.md",
    size: 1200,
    type: "text/markdown",
    content: "<h1>Report</h1><p>hello</p>",
    encoding: "utf-8",
    preview_type: "markdown",
    is_truncated: false,
    modified: "2026-02-08T12:00:00Z",
    metadata: {
      is_valid_json: null,
      is_valid_xml: null,
      line_count: 2,
    },
    ...overrides,
  };
}

function renderPanel(selection: { taskId?: number | null; filePath?: string | null } = {}) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={client}>
      <FilePreviewPanel taskId={selection.taskId ?? null} filePath={selection.filePath ?? null} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockApiFetch.mockReset();
  mockToast.mockReset();
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock-url");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  Object.assign(navigator, {
    clipboard: {
      writeText: vi.fn().mockResolvedValue(undefined),
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FilePreviewPanel", () => {
  it("shows empty state when no file is selected", () => {
    renderPanel({ taskId: 1 });
    expect(screen.getByText("Select a file to preview")).toBeTruthy();
    expect(mockApiFetch).not.toHaveBeenCalled();
  });

  it("fetches and renders markdown preview", async () => {
    mockApiFetch.mockResolvedValueOnce(jsonResponse(createPreviewPayload()));

    renderPanel({ taskId: 1, filePath: "/docs/report.md" });

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        "/api/tasks/1/files/content?path=%2Fdocs%2Freport.md",
        { method: "GET" },
      );
    });

    expect(await screen.findByText("Report")).toBeTruthy();
    expect(screen.getByText("/docs/report.md • 1.2 KB")).toBeTruthy();
  });

  it("renders JSON, XML, text, and binary modes", async () => {
    mockApiFetch
      .mockResolvedValueOnce(
        jsonResponse(
          createPreviewPayload({
            path: "/data.json",
            name: "data.json",
            preview_type: "json",
            type: "application/json",
            content: "{\"a\":1,\"b\":{\"c\":2}}",
            metadata: { is_valid_json: true, is_valid_xml: null, line_count: 1 },
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          createPreviewPayload({
            path: "/scan.xml",
            name: "scan.xml",
            preview_type: "xml",
            type: "application/xml",
            content: "<root><item key=\"v\">1</item></root>",
            metadata: { is_valid_json: null, is_valid_xml: true, line_count: 1 },
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          createPreviewPayload({
            path: "/note.txt",
            name: "note.txt",
            preview_type: "text",
            type: "text/plain",
            content: "line1\nline2",
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          createPreviewPayload({
            path: "/dump.bin",
            name: "dump.bin",
            preview_type: "binary",
            type: "application/octet-stream",
            content: "aGVsbG8=",
            encoding: "base64",
          }),
        ),
      );

    const first = renderPanel({ taskId: 1, filePath: "/data.json" });
    expect(await screen.findByText("JSON")).toBeTruthy();
    first.unmount();

    const second = renderPanel({ taskId: 1, filePath: "/scan.xml" });
    expect(await screen.findByText("scan.xml")).toBeTruthy();
    second.unmount();

    const third = renderPanel({ taskId: 1, filePath: "/note.txt" });
    expect(await screen.findByText(/line1/)).toBeTruthy();
    third.unmount();

    const fourth = renderPanel({ taskId: 1, filePath: "/dump.bin" });
    expect(await screen.findByText("Cannot preview this file type")).toBeTruthy();
    fourth.unmount();
  });

  it("copies path via clipboard button", async () => {
    mockApiFetch.mockResolvedValueOnce(jsonResponse(createPreviewPayload()));
    renderPanel({ taskId: 1, filePath: "/docs/report.md" });

    await screen.findByText("report.md");
    fireEvent.click(screen.getByLabelText("Copy file path"));

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("/docs/report.md");
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Path copied",
      }),
    );
  });

  it("shows error state and toast when preview request fails", async () => {
    mockApiFetch.mockResolvedValueOnce(jsonResponse({ detail: "Forbidden" }, 403));

    renderPanel({ taskId: 1, filePath: "/blocked.txt" });

    expect(await screen.findByText("Could not load this file preview.")).toBeTruthy();
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Failed to load file preview",
      }),
    );
  });

  it("downloads selected file when download button is clicked", async () => {
    mockApiFetch
      .mockResolvedValueOnce(jsonResponse(createPreviewPayload()))
      .mockResolvedValueOnce(new Response("file-content", { status: 200 }));

    renderPanel({ taskId: 1, filePath: "/docs/report.md" });
    await screen.findByText("report.md");

    fireEvent.click(screen.getByLabelText("Download file"));

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        "/api/tasks/1/files/download?path=%2Fdocs%2Freport.md",
        { method: "GET" },
      );
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Downloading report.md",
      }),
    );
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
  });
});
