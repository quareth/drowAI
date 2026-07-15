/**
 * Renders the tool-approval HITL surface.
 *
 * Phase 7 Task 7.6: when ``payload.items`` carries more than one entry the
 * card renders one row per committed call inside one batch context. Each
 * item exposes Approve / Edit / Skip controls; the parent receives a
 * structured response carrying per-item decisions so the orchestrator can
 * downgrade ``parallel`` → ``sequential`` on the survivor subset (Task 7.2).
 *
 * Single-item payloads (``items`` absent or length 1) preserve the existing
 * single-call appearance — no header, no per-row labels, identical hover
 * affordances. The batch UI is purely additive on top of the legacy shape.
 */

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Play, SquarePen, X } from "lucide-react";

import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import type { ToolApprovalItem, ToolApprovalPayload } from "@/types/hitl";

interface ToolApprovalCardProps {
  payload: ToolApprovalPayload;
  onApprove: () => void;
  onEdit: (editedParams: Record<string, unknown>) => void;
  onSkip: () => void;
  /**
   * Phase 7 multi-item callback. Optional so single-tool consumers that
   * still wire only ``onApprove``/``onEdit``/``onSkip`` continue to work
   * unchanged. When provided and the payload carries multiple items the
   * card invokes this with the full decision map.
   */
  onBatchSubmit?: (decisions: BatchApprovalDecisions) => void;
  isSubmitting?: boolean;
}

export type ToolApprovalAction = "approve" | "edit" | "skip";

export interface BatchApprovalDecision {
  tool_call_id?: string;
  action: ToolApprovalAction;
  edited_parameters?: Record<string, unknown>;
}

export interface BatchApprovalDecisions {
  action: "approve";
  tool_batch_id?: string;
  decisions: BatchApprovalDecision[];
}

interface InternalItemState {
  /** Stable per-row id used for React keys + decision lookups. */
  rowKey: string;
  item: ToolApprovalItem;
  draft: string;
  parseError: string | null;
  expanded: boolean;
  editMode: boolean;
  decision: ToolApprovalAction;
  editedParameters?: Record<string, unknown>;
}

function deriveItems(payload: ToolApprovalPayload): ToolApprovalItem[] {
  if (Array.isArray(payload.items) && payload.items.length > 0) {
    return payload.items;
  }
  return [
    {
      tool_call_id: undefined,
      tool_id: payload.tool_id,
      tool_name: payload.tool_name,
      parameters: payload.parameters,
      description: payload.description,
      risk_level: payload.risk_level,
    },
  ];
}

function makeRowKey(item: ToolApprovalItem, idx: number): string {
  return item.tool_call_id && item.tool_call_id.length > 0
    ? item.tool_call_id
    : `${item.tool_id}-${idx}`;
}

function riskColor(level?: ToolApprovalItem["risk_level"]) {
  if (!level) return null;
  const config: Record<"low" | "medium" | "high", { text: string; dot: string }> = {
    low: { text: "text-emerald-400/80", dot: "bg-emerald-400" },
    medium: { text: "text-amber-400/80", dot: "bg-amber-400" },
    high: { text: "text-rose-400/80", dot: "bg-rose-400" },
  };
  return config[level];
}

function buildState(items: ToolApprovalItem[]): InternalItemState[] {
  return items.map((item, idx) => ({
    rowKey: makeRowKey(item, idx),
    item,
    draft: JSON.stringify(item.parameters ?? {}, null, 2),
    parseError: null,
    expanded: false,
    editMode: false,
    decision: "approve",
  }));
}

interface ApprovalRowProps {
  state: InternalItemState;
  isMulti: boolean;
  isSubmitting: boolean;
  onToggleExpand: () => void;
  onApproveRow: () => void;
  onEditRowToggle: () => void;
  onSkipRow: () => void;
  onDraftChange: (value: string) => void;
}

function ApprovalRow({
  state,
  isMulti,
  isSubmitting,
  onToggleExpand,
  onApproveRow,
  onEditRowToggle,
  onSkipRow,
  onDraftChange,
}: ApprovalRowProps) {
  const risk = riskColor(state.item.risk_level);
  const decisionLabel: Record<ToolApprovalAction, { text: string; color: string }> = {
    approve: { text: "Approved", color: "text-emerald-400/80" },
    edit: { text: "Edited", color: "text-sky-400/80" },
    skip: { text: "Skipped", color: "text-slate-500" },
  };

  return (
    <div className="rounded-md">
      <div className="flex w-fit items-center gap-3 py-1.5 pl-3 pr-10">
        <button
          type="button"
          onClick={onToggleExpand}
          className="flex items-center gap-1.5 text-left"
        >
          <span className="font-mono text-xs text-slate-300">
            {state.item.tool_name.split(".").at(-1)}
          </span>
          {state.expanded ? (
            <ChevronUp className="h-3 w-3 text-slate-500" />
          ) : (
            <ChevronDown className="h-3 w-3 text-slate-500" />
          )}
        </button>

        {risk && (
          <span className={cn("flex items-center gap-1 text-[10px] uppercase", risk.text)}>
            <span className={cn("h-1.5 w-1.5 rounded-full", risk.dot)} />
            {state.item.risk_level}
          </span>
        )}

        <span className="h-3 w-px bg-slate-700/50" />

        <div className="flex items-center gap-0.5">
          <button
            type="button"
            onClick={onApproveRow}
            disabled={isSubmitting}
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-emerald-400 transition-colors hover:bg-emerald-500/10 disabled:opacity-40"
          >
            <Play className="h-2.5 w-2.5" />
            Run
          </button>
          <button
            type="button"
            onClick={onEditRowToggle}
            disabled={isSubmitting}
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-slate-400 transition-colors hover:bg-slate-700/50 hover:text-slate-300 disabled:opacity-40"
          >
            <SquarePen className="h-2.5 w-2.5" />
            {state.editMode ? "Save & Run" : "Edit"}
          </button>
          <button
            type="button"
            onClick={onSkipRow}
            disabled={isSubmitting}
            className="flex w-[50px] items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-slate-500 transition-colors hover:bg-slate-700/50 hover:text-slate-400 disabled:opacity-40"
          >
            <X className="h-2.5 w-2.5" />
            Skip
          </button>
        </div>

        {isMulti && (
          <span className={cn("text-[10px] uppercase", decisionLabel[state.decision].color)}>
            {decisionLabel[state.decision].text}
          </span>
        )}
      </div>

      {state.expanded && (
        <div className="border-t border-slate-700/30 px-2.5 py-2">
          {state.editMode ? (
            <div className="space-y-1.5">
              <Textarea
                value={state.draft}
                onChange={(event) => onDraftChange(event.target.value)}
                className="min-h-[80px] w-64 resize-none border-slate-700/50 bg-slate-950/60 font-mono text-[11px] text-slate-300 placeholder:text-slate-600"
              />
              {state.parseError && (
                <p className="text-[10px] text-rose-400">{state.parseError}</p>
              )}
            </div>
          ) : (
            <pre className="max-w-xs overflow-x-auto font-mono text-[11px] text-slate-400">
              {JSON.stringify(state.item.parameters, null, 2)}
            </pre>
          )}
          {state.item.description && (
            <p className="mt-1.5 max-w-xs text-[11px] text-slate-500">{state.item.description}</p>
          )}
        </div>
      )}
    </div>
  );
}

export function ToolApprovalCard({
  payload,
  onApprove,
  onEdit,
  onSkip,
  onBatchSubmit,
  isSubmitting = false,
}: ToolApprovalCardProps) {
  const items = useMemo(() => deriveItems(payload), [payload]);
  const isMulti = items.length > 1;
  const [rowStates, setRowStates] = useState<InternalItemState[]>(() => buildState(items));

  // Reset row state whenever the payload changes (new approval surface).
  useEffect(() => {
    setRowStates(buildState(items));
  }, [items]);

  const updateRow = (rowKey: string, updater: (row: InternalItemState) => InternalItemState) => {
    setRowStates((prev) =>
      prev.map((row) => (row.rowKey === rowKey ? updater(row) : row)),
    );
  };

  const buildBatchResponse = (next: InternalItemState[]): BatchApprovalDecisions => ({
    action: "approve",
    tool_batch_id: payload.tool_batch_id,
    decisions: next.map((row) => ({
      tool_call_id: row.item.tool_call_id,
      action: row.decision,
      edited_parameters: row.editedParameters,
    })),
  });

  const handleSubmitBatch = () => {
    if (!isMulti) return;
    if (!onBatchSubmit) return;
    onBatchSubmit(buildBatchResponse(rowStates));
  };

  const handleApproveRow = (rowKey: string) => {
    if (!isMulti) {
      onApprove();
      return;
    }
    setRowStates((prev) => {
      const next = prev.map((row) =>
        row.rowKey === rowKey
          ? { ...row, decision: "approve" as ToolApprovalAction, editedParameters: undefined }
          : row,
      );
      return next;
    });
  };

  const handleSkipRow = (rowKey: string) => {
    if (!isMulti) {
      onSkip();
      return;
    }
    setRowStates((prev) => {
      const next = prev.map((row) =>
        row.rowKey === rowKey ? { ...row, decision: "skip" as ToolApprovalAction } : row,
      );
      return next;
    });
  };

  const handleEditRowToggle = (rowKey: string) => {
    setRowStates((prev) => {
      const target = prev.find((row) => row.rowKey === rowKey);
      if (!target) return prev;

      // First click → enter edit mode.
      if (!target.editMode) {
        return prev.map((row) =>
          row.rowKey === rowKey
            ? {
                ...row,
                editMode: true,
                expanded: true,
                draft: JSON.stringify(row.item.parameters, null, 2),
                parseError: null,
              }
            : row,
        );
      }

      // Second click → "Save & Run".
      let parsed: Record<string, unknown> | null = null;
      let parseError: string | null = null;
      try {
        const candidate = JSON.parse(target.draft);
        if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) {
          parseError = "Parameters must be a JSON object.";
        } else {
          parsed = candidate as Record<string, unknown>;
        }
      } catch {
        parseError = "Invalid JSON syntax.";
      }

      if (parseError || !parsed) {
        return prev.map((row) =>
          row.rowKey === rowKey ? { ...row, parseError } : row,
        );
      }

      if (!isMulti) {
        onEdit(parsed);
        return prev.map((row) =>
          row.rowKey === rowKey
            ? { ...row, editMode: false, parseError: null, decision: "edit" }
            : row,
        );
      }

      const next = prev.map((row) =>
        row.rowKey === rowKey
          ? {
              ...row,
              editMode: false,
              parseError: null,
              decision: "edit" as ToolApprovalAction,
              editedParameters: parsed,
            }
          : row,
      );
      return next;
    });
  };

  const handleToggleExpand = (rowKey: string) => {
    updateRow(rowKey, (row) => ({ ...row, expanded: !row.expanded }));
  };

  const handleDraftChange = (rowKey: string, value: string) => {
    updateRow(rowKey, (row) => ({ ...row, draft: value }));
  };

  // Single-item path keeps the legacy single-call appearance unchanged.
  if (!isMulti) {
    const only = rowStates[0];
    return (
      <div className="inline-block rounded-md border border-slate-700/40 bg-slate-900/50 shadow-lg shadow-black/20 backdrop-blur-sm">
        <ApprovalRow
          state={only}
          isMulti={false}
          isSubmitting={isSubmitting}
          onToggleExpand={() => handleToggleExpand(only.rowKey)}
          onApproveRow={() => handleApproveRow(only.rowKey)}
          onEditRowToggle={() => handleEditRowToggle(only.rowKey)}
          onSkipRow={() => handleSkipRow(only.rowKey)}
          onDraftChange={(value) => handleDraftChange(only.rowKey, value)}
        />
      </div>
    );
  }

  // Multi-item path renders one batch container with per-row controls.
  return (
    <div
      data-testid="tool-approval-card-batch"
      className="inline-block rounded-md border border-slate-700/40 bg-slate-900/50 shadow-lg shadow-black/20 backdrop-blur-sm"
    >
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-slate-700/30 text-[11px] text-slate-400">
        <span className="font-medium uppercase tracking-wide">Batch approval</span>
        <span className="ml-auto text-slate-500">{rowStates.length} calls</span>
      </div>
      <div className="flex flex-col divide-y divide-slate-700/20">
        {rowStates.map((row) => (
          <ApprovalRow
            key={row.rowKey}
            state={row}
            isMulti
            isSubmitting={isSubmitting}
            onToggleExpand={() => handleToggleExpand(row.rowKey)}
            onApproveRow={() => handleApproveRow(row.rowKey)}
            onEditRowToggle={() => handleEditRowToggle(row.rowKey)}
            onSkipRow={() => handleSkipRow(row.rowKey)}
            onDraftChange={(value) => handleDraftChange(row.rowKey, value)}
          />
        ))}
      </div>
      <div className="flex justify-end border-t border-slate-700/30 px-3 py-2">
        <button
          type="button"
          onClick={handleSubmitBatch}
          disabled={isSubmitting}
          className="flex items-center gap-1 rounded bg-emerald-500/15 px-2 py-1 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/25 disabled:opacity-40"
        >
          <Play className="h-3 w-3" />
          Run selected
        </button>
      </div>
    </div>
  );
}

export default ToolApprovalCard;
