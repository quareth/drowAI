/**
 * Regression tests for the create-task dialog transport contract.
 *
 * Responsibilities:
 * - Ensure failed POST responses never trigger success UI.
 * - Ensure durable task responses update the canonical task-list cache by id.
 */
// @vitest-environment jsdom

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewTaskModal } from "@/components/modals/new-task-modal";
import { apiRequest, queryClient } from "@/lib/queryClient";
import type { Task } from "@/types";

const toastSpy = vi.fn();
const invalidateEngagementKnowledgeQueriesSpy = vi.fn();

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: toastSpy }),
}));

vi.mock("@/hooks/use-engagement-knowledge", () => ({
  invalidateEngagementKnowledgeQueries: (...args: unknown[]) =>
    invalidateEngagementKnowledgeQueriesSpy(...args),
}));

vi.mock("@/components/engagements/engagement-combobox", () => ({
  EngagementCombobox: () => <div data-testid="engagement-combobox" />,
}));

vi.mock("@/components/ui/file-drop-upload", () => ({
  FileDropUpload: () => null,
}));

vi.mock("@/components/vpn/VPNConfigForm", () => ({
  VPNConfigForm: () => <div data-testid="vpn-config-form" />,
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children, className }: { children: ReactNode; className?: string }) => (
    <div className={className} data-testid="dialog-content">
      {children}
    </div>
  ),
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({
    checked,
    disabled,
    onCheckedChange,
  }: {
    checked?: boolean;
    disabled?: boolean;
    onCheckedChange?: (checked: boolean) => void;
  }) => (
    <input
      aria-label="Enable VPN"
      checked={!!checked}
      disabled={disabled}
      onChange={(event) => onCheckedChange?.(event.currentTarget.checked)}
      type="checkbox"
    />
  ),
}));

vi.mock("@/lib/queryClient", async () => {
  const actual = await vi.importActual<typeof import("@/lib/queryClient")>("@/lib/queryClient");
  return {
    ...actual,
    apiRequest: vi.fn(),
  };
});

const apiRequestMock = vi.mocked(apiRequest);

function task(overrides: Partial<Task>): Task {
  return {
    id: 1,
    user_id: 1,
    name: "Task",
    status: "queued",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderModal(onOpenChange = vi.fn()) {
  render(
    <QueryClientProvider client={queryClient}>
      <NewTaskModal open={true} onOpenChange={onOpenChange} canCreateTask={true} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText("Task Name"), { target: { value: "New Task" } });
  fireEvent.click(screen.getByRole("button", { name: "Create Task" }));
  return { onOpenChange };
}

describe("NewTaskModal task creation contract", () => {
  beforeEach(() => {
    queryClient.clear();
    apiRequestMock.mockReset();
    toastSpy.mockReset();
    invalidateEngagementKnowledgeQueriesSpy.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it.each([
    [409, { detail: "No eligible runner is available for this task" }],
    [500, { detail: "Failed to create task" }],
  ])("does not show success or reset the modal for HTTP %s", async (status, body) => {
    const onOpenChange = vi.fn();
    apiRequestMock.mockResolvedValueOnce(jsonResponse(status, body));

    renderModal(onOpenChange);

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Failed to create task",
          variant: "destructive",
        }),
      );
    });
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(queryClient.getQueryData(["/api/tasks/"])).toBeUndefined();
    expect(apiRequestMock).toHaveBeenCalledTimes(1);
  });

  it("shows Runner Site readiness guidance for structured no-runner admission errors", async () => {
    apiRequestMock.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          reason_code: "NO_RUNNERS_REGISTERED",
          reason_codes: ["NO_RUNNERS_REGISTERED"],
          message: "No Runner is registered for this tenant.",
        },
      }),
    );

    renderModal();

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Runner Site needs a Runner",
          description:
            "No Runner is registered yet. Open Runner Site settings and connect a Runner before creating or starting tasks.",
          variant: "destructive",
        }),
      );
    });
  });

  it("does not present offline Runner admission errors as capacity exhaustion", async () => {
    apiRequestMock.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          reason_code: "RUNNER_STALE_OR_OFFLINE",
          reason_codes: ["RUNNER_STALE_OR_OFFLINE"],
          message: "Runner placement admission failed: RUNNER_STALE_OR_OFFLINE.",
        },
      }),
    );

    renderModal();

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Runner is not connected",
          variant: "destructive",
        }),
      );
    });
    expect(toastSpy).not.toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Runner capacity is exhausted",
      }),
    );
  });

  it("shows capacity exhaustion only for structured capacity reason codes", async () => {
    apiRequestMock.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          reason_code: "RUNNER_CAPACITY_EXHAUSTED",
          reason_codes: ["RUNNER_CAPACITY_EXHAUSTED"],
          message: "Runner active-task ceiling reached.",
        },
      }),
    );

    renderModal();

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Runner capacity is exhausted",
          description:
            "Connected Runners are at task capacity. Stop a running task or add another Runner, then try again.",
          variant: "destructive",
        }),
      );
    });
  });

  it("replaces runtime policy internals with admin-facing product wording", async () => {
    apiRequestMock.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          reason_code: "PRODUCT_RUNTIME_POLICY_INVALID",
          reason_codes: ["PRODUCT_RUNTIME_POLICY_INVALID"],
          message: "Set DROWAI_PRODUCT_RUNTIME_PLACEMENT=runner before creating tasks.",
        },
      }),
    );

    renderModal();

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Runtime policy needs admin attention",
          description:
            "Task runtime is not configured for Runner execution. Ask an administrator to review setup and Runner Site readiness.",
          variant: "destructive",
        }),
      );
    });
    expect(toastSpy).not.toHaveBeenCalledWith(
      expect.objectContaining({
        description: expect.stringContaining("DROWAI_PRODUCT_RUNTIME_PLACEMENT"),
      }),
    );
  });

  it("upserts a successful task response by id without duplicate cache entries", async () => {
    const updated = task({ id: 7, name: "Updated Task", status: "queued" });
    queryClient.setQueryData<Task[]>(["/api/tasks/"], [
      task({ id: 7, name: "Old Task", status: "created" }),
      task({ id: 8, name: "Other Task", status: "running" }),
    ]);
    apiRequestMock
      .mockResolvedValueOnce(jsonResponse(201, updated))
      .mockResolvedValueOnce(jsonResponse(200, { status: "ready" }));

    renderModal();

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Task created successfully" }),
      );
    });
    const cached = queryClient.getQueryData<Task[]>(["/api/tasks/"]);
    expect(cached?.map((item) => item.id)).toEqual([7, 8]);
    expect(cached?.[0].name).toBe("Updated Task");
    expect(apiRequestMock).toHaveBeenCalledWith("POST", "/api/tasks/", expect.any(Object));
    expect(apiRequestMock).toHaveBeenCalledWith("POST", "/api/tasks/7/chat/prewarm");
  });

  it("keeps a durable failed task visible and skips success prewarm", async () => {
    const failed = task({
      id: 9,
      status: "failed",
      error_message: "Runtime workspace materialization failed",
      failure_reason: "runtime_workspace_materialization_failed",
    });
    apiRequestMock.mockResolvedValueOnce(jsonResponse(201, failed));

    renderModal();

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Task created but startup failed",
          variant: "destructive",
        }),
      );
    });
    expect(queryClient.getQueryData<Task[]>(["/api/tasks/"])?.[0]).toMatchObject({
      id: 9,
      status: "failed",
    });
    expect(apiRequestMock).toHaveBeenCalledTimes(1);
  });

  it("bounds the dialog height and keeps task actions outside the scrollable form body", () => {
    render(
      <QueryClientProvider client={queryClient}>
        <NewTaskModal open={true} onOpenChange={vi.fn()} canCreateTask={true} />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByLabelText("Enable VPN"));

    expect(screen.getByTestId("dialog-content").className).toContain("max-h-[calc(100dvh-2rem)]");
    expect(screen.getByTestId("dialog-content").className).toContain("overflow-hidden");
    expect(screen.getByTestId("task-create-scroll-region").className).toContain("overflow-y-auto");
    expect(screen.getByTestId("vpn-config-form")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create Task" }).parentElement?.className).toContain("shrink-0");
  });
});
