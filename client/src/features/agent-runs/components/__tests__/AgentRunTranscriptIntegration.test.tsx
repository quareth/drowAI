// @vitest-environment jsdom
/**
 * Main-chat integration tests for compact Pathfinder agent-run cards.
 *
 * Responsibilities:
 * - verify lifecycle packets render as first-class activity cards
 * - prove stream hydration does not open drawer presentation state
 * - keep drawer opening behind explicit user activation
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tenantPermissionsMock = vi.hoisted(() => ({
  actions: ["task.control"] as string[],
}));

vi.mock("@/lib/api-config", () => ({
  apiFetch: vi.fn(),
}));
vi.mock("@/hooks/use-tenant-context", () => ({
  useTenantContext: () => ({
    effectivePermissions: { actions: tenantPermissionsMock.actions },
  }),
}));
vi.mock("@/hooks/use-user-timezone", () => ({
  useUserTimezone: () => "UTC",
}));
vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({ children }: { children?: ReactNode }) =>
    createElement("div", { "data-testid": "resizable-panel-group" }, children),
  ResizableHandle: (props: Record<string, unknown>) =>
    createElement("div", {
      "aria-label": props["aria-label"],
      "data-testid": "resizable-panel-handle",
    }),
  ResizablePanel: ({
    children,
    id,
    defaultSize,
    minSize,
    maxSize,
    collapsible,
    onCollapse,
  }: {
    children?: ReactNode;
    id?: string;
    defaultSize?: number;
    minSize?: number;
    maxSize?: number;
    collapsible?: boolean;
    onCollapse?: () => void;
  }) =>
    createElement(
      "div",
      {
        "data-testid": `resizable-panel-${id}`,
        "data-default-size": defaultSize,
        "data-min-size": minSize,
        "data-max-size": maxSize,
        "data-collapsible": collapsible,
      },
      children,
      id === "subagents-panel"
        ? createElement(
            "button",
            {
              type: "button",
              onClick: onCollapse,
              "data-testid": "collapse-subagents-panel",
            },
            "Collapse subagents",
          )
        : null,
    ),
}));

import { AgentRunTranscriptIntegration } from "../AgentRunTranscriptIntegration";
import type { ChatMessage } from "@/components/chat/types";
import { apiFetch } from "@/lib/api-config";
import { hydrateAgentRunStoreFromReplayItems } from "@/features/agent-runs/services/agent-run-replay-hydration";
import {
  applyAgentRunActivityPayload,
  applyAgentRunLifecycleUpdate,
  getAgentRunSnapshot,
  resetAgentRunStoreForTests,
} from "@/features/agent-runs/state/agent-stream-store";
import {
  getAgentRunPresentationSnapshot,
  resetAgentRunPresentationStoreForTests,
} from "@/features/agent-runs/state/agent-run-presentation-store";
import { clearTaskState, getTaskStreamSnapshot } from "@/state/chat-stream-store";
import type {
  AgentAssignment,
  AgentResultProjection,
  AgentRunLifecycleProjection,
} from "@/features/agent-runs/contracts/agent-run";
import {
  buildAgentAssignment,
  buildAgentResultProjection,
} from "@/features/agent-runs/test-data";

const TASK_ID = 61103;
const apiFetchMock = vi.mocked(apiFetch);

beforeEach(() => {
  tenantPermissionsMock.actions = ["task.control"];
  apiFetchMock.mockResolvedValue(localRunsResponse([localStatus()]));
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 0;
  });
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  resetAgentRunStoreForTests();
  resetAgentRunPresentationStoreForTests();
  clearTaskState(TASK_ID);
  apiFetchMock.mockReset();
  vi.unstubAllGlobals();
});

function assignment(overrides: Partial<AgentAssignment> = {}): AgentAssignment {
  return buildAgentAssignment({
    task_id: TASK_ID,
    assignment_id: "assignment-run-1",
    agent_run_id: "pathfinder-run-1",
    conversation_id: "conv-1",
    parent_turn_id: "turn-parent",
    objective: "Map exposed services",
    targets: ["10.0.0.10"],
    suggested_capabilities: ["port_scan"],
    scope_summary: "Targets: 10.0.0.10",
    ...overrides,
  });
}

function lifecycle(
  overrides: Partial<AgentRunLifecycleProjection> = {},
): AgentRunLifecycleProjection {
  return {
    agent_run_id: "pathfinder-run-1",
    agent_id: "pathfinder",
    agent_kind: "recon",
    agent_display_name: "Pathfinder",
    status: "running",
    lifecycle_version: 1,
    task_id: TASK_ID,
    conversation_id: "conv-1",
    parent_turn_id: "turn-parent",
    parent_run_id: "parent-run-1",
    assignment: assignment(),
    result: null,
    safe_error: null,
    ...overrides,
  };
}

function completedResult(
  overrides: Partial<AgentResultProjection> = {},
): AgentResultProjection {
  const { agent_run_id, agent_id, agent_kind, ...resultOverrides } = overrides;
  return buildAgentResultProjection({
    assignment: assignment({
      agent_run_id: agent_run_id ?? "pathfinder-run-1",
      agent_id: agent_id ?? "pathfinder",
      agent_kind: agent_kind ?? "recon",
    }),
    agent_display_name: "Pathfinder",
    summary: "Pathfinder found HTTPS on 443.",
    key_findings: ["HTTPS exposed on 443"],
    evidence_refs: [{ path: "/workspace/task-42/nmap.xml" }],
    tools_used: ["information_gathering.network_discovery.nmap"],
    limitations: ["Single approved target only."],
    recommended_next_steps: ["Review the HTTPS service banner."],
    final_checkpoint_id: "cp-pathfinder-final",
    ...resultOverrides,
  });
}

function lifecycleMessage(): ChatMessage {
  return {
    id: "lifecycle-1",
    type: "agent",
    content: "agent_run_lifecycle",
    timestamp: "2026-01-01T00:00:00Z",
    isStreaming: false,
    metadata: {
      id: "turn-parent",
      subtype: "agent_run_lifecycle",
      producer_type: "subagent",
      agent_run_id: "pathfinder-run-1",
      agent_id: "pathfinder",
      agent_kind: "recon",
      parent_turn_id: "turn-parent",
      parent_run_id: "parent-run-1",
      turn_sequence: 1,
      ind: -1,
    },
  };
}

function reconParentAcknowledgementMessage(): ChatMessage {
  return {
    id: "turn-parent",
    type: "agent",
    content: "Pathfinder has started a recon run and will hand off findings when it finishes.",
    timestamp: "2026-01-01T00:00:00Z",
    isStreaming: false,
    metadata: {
      id: "turn-parent",
      role: "assistant",
      branch: "subagent",
      agent_run_id: "pathfinder-run-1",
      agent_id: "pathfinder",
      agent_kind: "recon",
      agent_display_name: "Pathfinder",
      graph_thread_id: "thread-pathfinder",
      status: "running",
      turn_sequence: 1,
      ind: -1,
    },
  };
}

function pathfinderActivityMessage(
  type: string,
  content: string,
  sequence: number,
  metadata: Record<string, unknown> = {},
): ChatMessage {
  return {
    id: `pathfinder-activity-${sequence}`,
    type: "agent",
    content,
    timestamp: "2026-01-01T00:00:00Z",
    isStreaming: type.endsWith("_delta"),
    metadata: {
      id: "child-turn",
      producer_type: "subagent",
      agent_run_id: "pathfinder-run-1",
      agent_id: "pathfinder",
      agent_kind: "recon",
      agent_display_name: "Pathfinder",
      parent_turn_id: "turn-parent",
      parent_run_id: "parent-run-1",
      step_type: type,
      sequence,
      turn_sequence: 1,
      ...metadata,
    },
  };
}

function localStatus(
  overrides: Partial<AgentRunLifecycleProjection> = {},
): Record<string, unknown> {
  return {
    ...lifecycle(overrides),
    assignment: assignment(),
    cancel_requested: false,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    completed_at: null,
  };
}

function localRunsResponse(agentRuns: Record<string, unknown>[]): Response {
  return {
    ok: true,
    json: async () => ({
      process_local: true,
      task_id: TASK_ID,
      agent_runs: agentRuns,
    }),
  } as Response;
}

function lifecycleReplayPacket(sequence: number): Record<string, unknown> {
  return {
    type: "status",
    content: "agent_run_lifecycle",
    task_id: TASK_ID,
    sequence,
    metadata: {
      subtype: "agent_run_lifecycle",
      producer_type: "subagent",
      agent_run_id: "pathfinder-run-1",
      agent_id: "pathfinder",
      agent_kind: "recon",
      agent_display_name: "Pathfinder",
      parent_turn_id: "turn-parent",
      parent_run_id: "parent-run-1",
      internal_only: false,
      lifecycle_version: 1,
      sequence,
    },
    agent_run: lifecycle(),
  };
}

describe("AgentRunTranscriptIntegration Pathfinder agent-run cards", () => {
  it.each([
    ["viewer", [], false],
    ["operator", ["task.control"], true],
  ])("shows Stop for %s according to task.control permission", (_role, actions, expected) => {
    tenantPermissionsMock.actions = actions;
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[lifecycleMessage()]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /open subagents/i }));
    fireEvent.click(screen.getByTestId("agent-run-row-pathfinder-run-1"));

    const stopButton = screen.queryByRole("button", { name: "Stop" });
    expect(Boolean(stopButton)).toBe(expected);
  });

  it("renders a stored Pathfinder run after reasoning inside the parent activity group", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[
          {
            id: "turn-parent",
            type: "user",
            content: "Use Pathfinder to map exposed services.",
            timestamp: "2026-01-01T00:00:00Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 1,
              sequence: 10,
              conversation_id: "conv-1",
            },
          },
          {
            id: "turn-parent-reasoning",
            type: "agent",
            content: "Delegating the scoped scan.",
            timestamp: "2026-01-01T00:00:01Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 1,
              sequence: 11,
              ind: 0,
              step_type: "reasoning_delta",
              reasoning_section_id: "turn-parent:reasoning:0",
              conversation_id: "conv-1",
            },
          },
          {
            id: "turn-parent-reasoning-end",
            type: "agent",
            content: "",
            timestamp: "2026-01-01T00:00:01Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 1,
              sequence: 11,
              ind: 0,
              step_type: "reasoning_section_end",
              reasoning_section_id: "turn-parent:reasoning:0",
              conversation_id: "conv-1",
            },
          },
          {
            id: "turn-parent-final",
            type: "agent",
            content: "The requested scan is complete.",
            timestamp: "2026-01-01T00:00:02Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 1,
              sequence: 20,
              step_type: "assistant_message",
              conversation_id: "conv-1",
            },
          },
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.queryByTestId("agent-run-card-pathfinder-run-1")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "1 thought, 1 agent" }),
    );

    const activityDetails = screen.getByTestId("turn-activity-details-turn-sequence:1");
    const reasoning = screen.getByTestId(
      "reasoning-step-0:reasoning-turn-parent:reasoning:0",
    );
    const pathfinder = screen.getByTestId("agent-run-card-pathfinder-run-1");
    expect(activityDetails.contains(reasoning)).toBe(true);
    expect(activityDetails.contains(pathfinder)).toBe(true);
    expect(activityDetails.dataset.activityChain).toBe("true");
    expect(screen.getByTestId("turn-activity-details-turn-sequence:1-node-0")).toBeTruthy();
    expect(screen.getByTestId("turn-activity-details-turn-sequence:1-connector-0")).toBeTruthy();
    expect(
      reasoning.compareDocumentPosition(pathfinder) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByTestId("agent-run-card-pathfinder-run-1")).toBeTruthy();
  });

  it("uses the shared connected activity chain before the parent turn completes", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[
          {
            id: "turn-parent-live-reasoning-start",
            type: "agent",
            content: "",
            timestamp: "2026-01-01T00:00:01Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 1,
              sequence: 10,
              ind: 0,
              step_type: "reasoning_start",
              reasoning_section_id: "turn-parent:reasoning:0",
              conversation_id: "conv-1",
            },
          },
          {
            id: "turn-parent-live-reasoning-delta",
            type: "agent",
            content: "Delegating the scoped scan.",
            timestamp: "2026-01-01T00:00:02Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 1,
              sequence: 11,
              ind: 0,
              step_type: "reasoning_delta",
              reasoning_section_id: "turn-parent:reasoning:0",
              conversation_id: "conv-1",
            },
          },
          {
            id: "turn-parent-live-reasoning-end",
            type: "agent",
            content: "",
            timestamp: "2026-01-01T00:00:03Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 1,
              sequence: 11,
              ind: 0,
              step_type: "reasoning_section_end",
              reasoning_section_id: "turn-parent:reasoning:0",
              conversation_id: "conv-1",
            },
          },
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    const activityDetails = screen.getByTestId(
      "turn-activity-details-turn-sequence:1",
    );
    const reasoning = screen.getByTestId(
      "reasoning-step-0:reasoning-turn-parent:reasoning:0",
    );
    const pathfinder = screen.getByTestId("agent-run-card-pathfinder-run-1");
    expect(activityDetails.contains(reasoning)).toBe(true);
    expect(activityDetails.contains(pathfinder)).toBe(true);
    expect(
      screen.getByTestId("turn-activity-details-turn-sequence:1-connector-0"),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "1 thought, 1 agent" })).toBeNull();
  });

  it("orders different subagent kinds by their lifecycle sequence between reasoning steps", () => {
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        agent_run_id: "research-run-1",
        agent_id: "researcher",
        agent_kind: "research",
        agent_display_name: "Researcher",
        assignment: null,
      }),
      12,
    );
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        agent_run_id: "review-run-1",
        agent_id: "reviewer",
        agent_kind: "review",
        agent_display_name: "Reviewer",
        assignment: null,
      }),
      15,
    );

    render(
      <AgentRunTranscriptIntegration
        messages={[
          {
            id: "turn-parent-reasoning-1",
            type: "agent",
            content: "Selecting a research delegate.",
            timestamp: "2026-01-01T00:00:01Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 2,
              sequence: 10,
              ind: 0,
              sub_turn_index: 0,
              step_type: "reasoning_delta",
              reasoning_section_id: "turn-parent:reasoning:0",
            },
          },
          {
            id: "turn-parent-reasoning-1-end",
            type: "agent",
            content: "",
            timestamp: "2026-01-01T00:00:01Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 2,
              sequence: 11,
              ind: 0,
              sub_turn_index: 0,
              step_type: "reasoning_section_end",
              reasoning_section_id: "turn-parent:reasoning:0",
            },
          },
          {
            id: "turn-parent-reasoning-2",
            type: "agent",
            content: "Sending the findings for review.",
            timestamp: "2026-01-01T00:00:02Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 2,
              sequence: 13,
              ind: 0,
              sub_turn_index: 1,
              step_type: "reasoning_delta",
              reasoning_section_id: "turn-parent:reasoning:1",
            },
          },
          {
            id: "turn-parent-reasoning-2-end",
            type: "agent",
            content: "",
            timestamp: "2026-01-01T00:00:02Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 2,
              sequence: 14,
              ind: 0,
              sub_turn_index: 1,
              step_type: "reasoning_section_end",
              reasoning_section_id: "turn-parent:reasoning:1",
            },
          },
          {
            id: "turn-parent-final",
            type: "agent",
            content: "The delegated work is complete.",
            timestamp: "2026-01-01T00:00:03Z",
            isStreaming: false,
            metadata: {
              id: "turn-parent",
              turn_sequence: 2,
              sequence: 20,
              ind: 2,
              step_type: "assistant_message",
            },
          },
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "2 thoughts, 2 agents" }),
    );

    const details = screen.getByTestId("turn-activity-details-turn-sequence:2");
    const orderedEvents = [
      screen.getByTestId("reasoning-step-0:reasoning-turn-parent:reasoning:0"),
      screen.getByTestId("agent-run-card-research-run-1"),
      screen.getByTestId("reasoning-step-0:reasoning-turn-parent:reasoning:1"),
      screen.getByTestId("agent-run-card-review-run-1"),
    ];
    expect(orderedEvents.every((event) => details.contains(event))).toBe(true);
    for (let index = 0; index < orderedEvents.length - 1; index += 1) {
      expect(
        orderedEvents[index].compareDocumentPosition(orderedEvents[index + 1]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
    expect(screen.getByText("Researcher")).toBeTruthy();
    expect(screen.getByText("Reviewer")).toBeTruthy();
  });

  it("renders a card from replayed lifecycle markers when chat history has no lifecycle row", () => {
    hydrateAgentRunStoreFromReplayItems(TASK_ID, [
      lifecycleReplayPacket(31),
      {
        type: "reasoning_delta",
        content: "replayed Pathfinder reasoning",
        task_id: TASK_ID,
        sequence: 32,
        metadata: {
          id: "child-turn",
          ind: 0,
          step_type: "reasoning_delta",
          reasoning_section_id: "child-turn:reasoning:0",
          turn_sequence: 1,
          producer_type: "subagent",
          agent_run_id: "pathfinder-run-1",
          agent_id: "pathfinder",
          agent_kind: "recon",
          agent_display_name: "Pathfinder",
          parent_turn_id: "turn-parent",
          parent_run_id: "parent-run-1",
          sequence: 32,
        },
      },
    ], 32);
    const replayedMessages = getTaskStreamSnapshot(TASK_ID).items as unknown as ChatMessage[];

    render(
      <AgentRunTranscriptIntegration
        messages={replayedMessages}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.getByTestId("agent-run-card-pathfinder-run-1")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /open subagents for map exposed services/i }),
    ).toBeTruthy();
    expect(screen.queryByText("replayed Pathfinder reasoning")).toBeNull();
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });
  });

  it("renders a real recon parent acknowledgement as the compact card", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[reconParentAcknowledgementMessage()]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.getByTestId("agent-run-card-pathfinder-run-1")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /open subagents for map exposed services/i }),
    ).toBeTruthy();
    expect(
      screen.queryByText("Pathfinder has started a recon run and will hand off findings when it finishes."),
    ).toBeNull();
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });
  });

  it("opens replayed runs by parent turn when parent_run_id is missing", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle({ parent_run_id: null }), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[
          {
            ...lifecycleMessage(),
            metadata: {
              ...lifecycleMessage().metadata,
              parent_run_id: undefined,
            },
          },
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /open subagents/i }));

    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: true,
      parentRunId: "turn-parent",
      view: "list",
    });
    expect(screen.getByTestId("agent-run-row-pathfinder-run-1")).toBeTruthy();
    expect(screen.queryByText("No subagent runs are available for this turn.")).toBeNull();
  });

  it("renders a lifecycle packet as a compact card without opening the drawer", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);
    applyAgentRunActivityPayload(TASK_ID, {
      type: "reasoning_delta",
      content: "internal chain of thought",
      sequence: 13,
      task_id: TASK_ID,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "pathfinder-run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        agent_display_name: "Pathfinder",
        parent_turn_id: "turn-parent",
        parent_run_id: "parent-run-1",
      },
    });

    render(
      <AgentRunTranscriptIntegration
        messages={[lifecycleMessage()]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.getByTestId("agent-run-card-pathfinder-run-1")).toBeTruthy();
    expect(screen.getByText("Pathfinder")).toBeTruthy();
    expect(screen.getByText("working")).toBeTruthy();
    expect(screen.queryByText("agent_run_lifecycle")).toBeNull();
    expect(screen.queryByText("internal chain of thought")).toBeNull();
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: false,
      parentRunId: null,
      view: "list",
      selectedAgentRunId: null,
    });
  });

  it("opens the contained subagent list before selecting the Pathfinder thread", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[
          lifecycleMessage(),
          pathfinderActivityMessage("reasoning_start", "", 13, {
            ind: 0,
            reasoning_section_id: "pathfinder-reasoning-1",
          }),
          pathfinderActivityMessage("reasoning_delta", "checking exposed services", 14, {
            ind: 0,
            reasoning_section_id: "pathfinder-reasoning-1",
          }),
          pathfinderActivityMessage("reasoning_section_end", "", 15, {
            ind: 0,
            reasoning_section_id: "pathfinder-reasoning-1",
          }),
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    const conversation = screen.getByRole("region", { name: /conversation history/i });
    fireEvent.click(screen.getByRole("button", { name: /open subagents/i }));

    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: true,
      parentRunId: "parent-run-1",
      view: "list",
      selectedAgentRunId: null,
      activityExpanded: false,
    });
    const panel = screen.getByRole("complementary", { name: /subagents/i });
    expect(conversation.contains(panel)).toBe(true);
    expect(screen.getByTestId("agent-run-list")).toBeTruthy();
    expect(screen.getByText("Active · 1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /open pathfinder thread/i }));

    expect(screen.getByTestId("agent-run-detail")).toBeTruthy();
    expect(screen.getByTestId("agent-activity-timeline")).toBeTruthy();
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      view: "detail",
      selectedAgentRunId: "pathfinder-run-1",
    });
  });

  it("lists repeated Pathfinder invocations from separate turns in the same conversation", () => {
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        agent_run_id: "pathfinder-run-port-80",
        status: "completed",
        lifecycle_version: 2,
        parent_turn_id: "turn-port-80",
        parent_run_id: "parent-run-port-80",
        assignment: assignment({
          assignment_id: "assignment-port-80",
          agent_run_id: "pathfinder-run-port-80",
          parent_turn_id: "turn-port-80",
          objective: "Scan port 80",
        }),
        result: completedResult({
          agent_run_id: "pathfinder-run-port-80",
          summary: "Port 80 is closed.",
        }),
      }),
      12,
    );
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        agent_run_id: "pathfinder-run-port-443",
        status: "completed",
        lifecycle_version: 2,
        parent_turn_id: "turn-port-443",
        parent_run_id: "parent-run-port-443",
        assignment: assignment({
          assignment_id: "assignment-port-443",
          agent_run_id: "pathfinder-run-port-443",
          parent_turn_id: "turn-port-443",
          objective: "Scan port 443",
        }),
        result: completedResult({
          agent_run_id: "pathfinder-run-port-443",
          summary: "Port 443 is closed.",
        }),
      }),
      32,
    );
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        agent_run_id: "pathfinder-run-other-conversation",
        status: "completed",
        lifecycle_version: 2,
        conversation_id: "conv-other",
        parent_turn_id: "turn-other-conversation",
        parent_run_id: "parent-run-other-conversation",
        assignment: assignment({
          assignment_id: "assignment-other-conversation",
          agent_run_id: "pathfinder-run-other-conversation",
          conversation_id: "conv-other",
          parent_turn_id: "turn-other-conversation",
          objective: "Scan another conversation",
        }),
      }),
      52,
    );

    const firstLifecycleMessage: ChatMessage = {
      ...lifecycleMessage(),
      id: "lifecycle-port-80",
      metadata: {
        ...lifecycleMessage().metadata,
        id: "turn-port-80",
        agent_run_id: "pathfinder-run-port-80",
        parent_turn_id: "turn-port-80",
        parent_run_id: "parent-run-port-80",
        turn_sequence: 1,
        sequence: 12,
      },
    };
    const secondLifecycleMessage: ChatMessage = {
      ...lifecycleMessage(),
      id: "lifecycle-port-443",
      metadata: {
        ...lifecycleMessage().metadata,
        id: "turn-port-443",
        agent_run_id: "pathfinder-run-port-443",
        parent_turn_id: "turn-port-443",
        parent_run_id: "parent-run-port-443",
        turn_sequence: 2,
        sequence: 32,
      },
    };
    const firstActivityMessage = pathfinderActivityMessage(
      "reasoning_delta",
      "Reasoning for port 80",
      13,
      {
        agent_run_id: "pathfinder-run-port-80",
        parent_turn_id: "turn-port-80",
        parent_run_id: "parent-run-port-80",
        reasoning_section_id: "port-80-reasoning",
      },
    );
    const secondActivityMessage = pathfinderActivityMessage(
      "reasoning_delta",
      "Reasoning for port 443",
      33,
      {
        agent_run_id: "pathfinder-run-port-443",
        parent_turn_id: "turn-port-443",
        parent_run_id: "parent-run-port-443",
        reasoning_section_id: "port-443-reasoning",
      },
    );

    render(
      <AgentRunTranscriptIntegration
        messages={[
          firstLifecycleMessage,
          firstActivityMessage,
          secondLifecycleMessage,
          secondActivityMessage,
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Open subagents for Scan port 443" }),
    );

    expect(screen.getByText("Done · 2")).toBeTruthy();
    expect(screen.getByTestId("agent-run-row-pathfinder-run-port-80")).toBeTruthy();
    expect(screen.getByTestId("agent-run-row-pathfinder-run-port-443")).toBeTruthy();
    expect(
      screen.queryByTestId("agent-run-row-pathfinder-run-other-conversation"),
    ).toBeNull();

    fireEvent.click(screen.getByTestId("agent-run-row-pathfinder-run-port-80"));
    expect(getAgentRunPresentationSnapshot(TASK_ID).selectedAgentRunId).toBe(
      "pathfinder-run-port-80",
    );
    expect(screen.getByText("Reasoning for port 80")).toBeTruthy();
    expect(screen.queryByText("Reasoning for port 443")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Back to subagent list" }));
    fireEvent.click(screen.getByTestId("agent-run-row-pathfinder-run-port-443"));
    expect(getAgentRunPresentationSnapshot(TASK_ID).selectedAgentRunId).toBe(
      "pathfinder-run-port-443",
    );
    expect(screen.getByText("Reasoning for port 443")).toBeTruthy();
    expect(screen.queryByText("Reasoning for port 80")).toBeNull();
  });

  it("limits Pathfinder resizing and closes instead of keeping a narrow panel", async () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[lifecycleMessage()]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /open subagents/i }));

    const subagentsPanel = screen.getByTestId("resizable-panel-subagents-panel");
    expect(subagentsPanel.getAttribute("data-default-size")).toBe("44");
    expect(subagentsPanel.getAttribute("data-min-size")).toBe("36");
    expect(subagentsPanel.getAttribute("data-max-size")).toBe("44");
    expect(subagentsPanel.getAttribute("data-collapsible")).toBe("true");
    expect(
      screen.getByLabelText("Resize subagents panel"),
    ).toBeTruthy();

    fireEvent.click(screen.getByTestId("collapse-subagents-panel"));

    await waitFor(() => {
      expect(getAgentRunPresentationSnapshot(TASK_ID).isOpen).toBe(false);
    });
    expect(screen.queryByRole("complementary", { name: /subagents/i })).toBeNull();
  });

  it("shows Pathfinder reasoning after selecting Pathfinder from the subagent list", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle(), 12);
    applyAgentRunActivityPayload(TASK_ID, {
      type: "reasoning_start",
      content: "",
      sequence: 13,
      task_id: TASK_ID,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "pathfinder-run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        agent_display_name: "Pathfinder",
        parent_turn_id: "turn-parent",
        parent_run_id: "parent-run-1",
        step_type: "reasoning_start",
        reasoning_section_id: "pathfinder-reasoning-1",
        ind: 0,
      },
    });
    applyAgentRunActivityPayload(TASK_ID, {
      type: "reasoning_delta",
      content: "checking exposed services",
      sequence: 14,
      task_id: TASK_ID,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "pathfinder-run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        agent_display_name: "Pathfinder",
        parent_turn_id: "turn-parent",
        parent_run_id: "parent-run-1",
        step_type: "reasoning_delta",
        reasoning_section_id: "pathfinder-reasoning-1",
        ind: 0,
      },
    });
    applyAgentRunActivityPayload(TASK_ID, {
      type: "reasoning_section_end",
      content: "",
      sequence: 15,
      task_id: TASK_ID,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "pathfinder-run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        agent_display_name: "Pathfinder",
        parent_turn_id: "turn-parent",
        parent_run_id: "parent-run-1",
        step_type: "reasoning_section_end",
        reasoning_section_id: "pathfinder-reasoning-1",
        ind: 0,
      },
    });

    render(
      <AgentRunTranscriptIntegration
        messages={[
          lifecycleMessage(),
          pathfinderActivityMessage("reasoning_start", "", 13, {
            ind: 0,
            reasoning_section_id: "pathfinder-reasoning-1",
          }),
          pathfinderActivityMessage("reasoning_delta", "checking exposed services", 14, {
            ind: 0,
            reasoning_section_id: "pathfinder-reasoning-1",
          }),
          pathfinderActivityMessage("reasoning_section_end", "", 15, {
            ind: 0,
            reasoning_section_id: "pathfinder-reasoning-1",
          }),
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /open subagents/i }));
    fireEvent.click(screen.getByRole("button", { name: /open pathfinder thread/i }));

    expect(screen.getByTestId("agent-run-detail")).toBeTruthy();
    expect(screen.getByTestId("agent-activity-timeline")).toBeTruthy();
    expect(screen.getByTestId("agent-activity-timeline").dataset.activityChain).toBe("true");
    expect(screen.getByTestId("agent-activity-timeline-node-0")).toBeTruthy();
    expect(screen.getByTestId("reasoning-step-0:reasoning-pathfinder-reasoning-1")).toBeTruthy();
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      view: "detail",
      selectedAgentRunId: "pathfinder-run-1",
      activityExpanded: false,
    });
  });

  it("renders only the terminal handoff after streamed child activity", () => {
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle({ status: "running" }), 12);
    applyAgentRunActivityPayload(TASK_ID, {
      type: "tool_batch_start",
      content: "nmap",
      sequence: 13,
      task_id: TASK_ID,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "pathfinder-run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        agent_display_name: "Pathfinder",
        parent_turn_id: "turn-parent",
        parent_run_id: "parent-run-1",
        tool_batch_id: "batch-pathfinder-1",
      },
    });

    const { rerender } = render(
      <AgentRunTranscriptIntegration
        messages={[reconParentAcknowledgementMessage()]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.getByText("working")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /open subagents/i }));
    fireEvent.click(screen.getByRole("button", { name: /open pathfinder thread/i }));
    expect(screen.getByTestId("agent-run-detail")).toBeTruthy();
    expect(screen.queryByText("Pathfinder found HTTPS on 443.")).toBeNull();

    applyAgentRunActivityPayload(TASK_ID, {
      type: "message_start",
      content: "",
      sequence: 14,
      task_id: TASK_ID,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "pathfinder-run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        agent_display_name: "Pathfinder",
        parent_turn_id: "turn-parent",
        parent_run_id: "parent-run-1",
        step_type: "message_start",
        ind: 3,
      },
    });
    applyAgentRunActivityPayload(TASK_ID, {
      type: "message_delta",
      content: "Streamed Pathfinder handoff.",
      sequence: 15,
      task_id: TASK_ID,
      metadata: {
        producer_type: "subagent",
        agent_run_id: "pathfinder-run-1",
        agent_id: "pathfinder",
        agent_kind: "recon",
        agent_display_name: "Pathfinder",
        parent_turn_id: "turn-parent",
        parent_run_id: "parent-run-1",
        step_type: "message_delta",
        ind: 3,
      },
    });
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        status: "completed",
        lifecycle_version: 3,
        assignment: null,
        result: completedResult(),
      }),
      17,
    );
    rerender(
      <AgentRunTranscriptIntegration
        messages={[
          reconParentAcknowledgementMessage(),
          pathfinderActivityMessage("message_start", "", 14, { ind: 2 }),
          pathfinderActivityMessage("message_delta", "Streamed Pathfinder handoff.", 15, {
            ind: 2,
          }),
          pathfinderActivityMessage("message_section_end", "", 16, {
            ind: 2,
            streaming: false,
          }),
        ]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.getByText("completed")).toBeTruthy();
    const conversation = screen.getByRole("region", {
      name: "Pathfinder conversation",
    });
    const finalMessage = screen.getByRole("article", {
      name: "Pathfinder final message",
    });
    expect(conversation.textContent).toContain("Streamed Pathfinder handoff.");
    expect(finalMessage.textContent).toContain("Pathfinder found HTTPS on 443.");
    expect(finalMessage.textContent).not.toContain("HTTPS exposed on 443");
    expect(finalMessage.textContent).not.toContain("Single approved target only.");
    expect(finalMessage.textContent).not.toContain(
      "Review the HTTPS service banner.",
    );
    expect(screen.queryByText("Streaming response…")).toBeNull();
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: true,
      view: "detail",
      selectedAgentRunId: "pathfinder-run-1",
      activityExpanded: false,
    });
  });

  it("shows waiting status without approval controls in the selected contained panel", async () => {
    applyAgentRunLifecycleUpdate(
      TASK_ID,
      lifecycle({
        status: "waiting_for_approval",
        lifecycle_version: 2,
      }),
      12,
    );

    render(
      <AgentRunTranscriptIntegration
        messages={[lifecycleMessage()]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.queryByText("Approval")).toBeNull();
    const cardButton = screen.getByRole("button", {
      name: /open subagents for map exposed services/i,
    });
    cardButton.focus();
    expect(document.activeElement).toBe(cardButton);
    fireEvent.click(cardButton);
    fireEvent.click(screen.getByRole("button", { name: /open pathfinder thread/i }));

    expect(screen.getByRole("complementary", { name: /subagents/i })).toBeTruthy();
    expect(
      screen.getByText("Pathfinder is waiting for a tool approval."),
    ).toBeTruthy();
    expect(screen.queryByText("Approval")).toBeNull();
    expect(screen.queryByRole("button", { name: /^run$/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /close subagents/i }));
    await waitFor(() => {
      expect(getAgentRunPresentationSnapshot(TASK_ID).isOpen).toBe(false);
    });
  });

  it("marks replayed nonterminal Pathfinder runs interrupted when the local registry lost them", async () => {
    apiFetchMock.mockResolvedValueOnce(localRunsResponse([]));
    applyAgentRunLifecycleUpdate(TASK_ID, lifecycle({ status: "running" }), 12);

    render(
      <AgentRunTranscriptIntegration
        messages={[lifecycleMessage()]}
        taskId={TASK_ID}
        isLoading={false}
        isConnected
      />,
    );

    expect(screen.getByText("working")).toBeTruthy();

    await waitFor(() => {
      expect(getAgentRunSnapshot(TASK_ID).runsById["pathfinder-run-1"].status).toBe("interrupted");
    });

    expect(screen.getByText("interrupted")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /open subagents/i }));
    fireEvent.click(screen.getByRole("button", { name: /open pathfinder thread/i }));
    expect(
      screen.getAllByText(/current backend process no longer owns it/i).length,
    ).toBeGreaterThan(0);
    expect(getAgentRunPresentationSnapshot(TASK_ID)).toMatchObject({
      isOpen: true,
      parentRunId: "parent-run-1",
      view: "detail",
      selectedAgentRunId: "pathfinder-run-1",
      activityExpanded: false,
    });
    expect(apiFetchMock).toHaveBeenCalledWith(
      `/api/tasks/${TASK_ID}/agent-runs/local`,
      expect.objectContaining({ method: "GET" }),
    );
  });
});
