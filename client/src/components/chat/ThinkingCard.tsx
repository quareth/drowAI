/**
 * Minimal Thinking card used for reasoning/analysis phases.
 *
 * Receives normalized reasoning sections and renders a dim, collapsible
 * progress timeline that keeps attention on the final answer bubble.
 */

import { Brain, Loader2 } from "lucide-react";

import { useCardToggleState } from "@/hooks/useCardToggleState";

interface ThinkingCardProps {
  steps: string[];
  defaultOpen?: boolean;
  isInProgress?: boolean;
  durationMs?: number;
  /** Stable identifier so collapse/expand state persists across remounts. */
  stateKey?: string;
  testId?: string;
}

function formatDurationLabel(durationMs: number): string {
  if (!Number.isFinite(durationMs) || durationMs < 0) {
    return "Thought";
  }
  if (durationMs < 10_000) {
    return `Thought for ${(Math.round(durationMs / 100) / 10).toFixed(1)}s`;
  }
  return `Thought for ${Math.round(durationMs / 1000)}s`;
}

export function ThinkingCard({
  steps,
  defaultOpen = false,
  isInProgress = false,
  durationMs,
  stateKey,
  testId,
}: ThinkingCardProps) {
  const visibleSteps = steps.map((step) => step.trim()).filter(Boolean);
  const hasContent = visibleSteps.length > 0;
  const [isOpen, setIsOpen] = useCardToggleState(stateKey, defaultOpen);
  const isBodyOpen = hasContent && isOpen;
  const latestStep = visibleSteps.at(-1)?.replace(/\s+/g, " ");
  const headerLabel = isInProgress
    ? "Thinking"
    : typeof durationMs === "number"
      ? formatDurationLabel(durationMs)
      : "Thought";
  const headerDetail = isInProgress
    ? latestStep
    : visibleSteps.length > 1
      ? `${visibleSteps.length} steps`
      : undefined;

  return (
    <div
      className="mb-1 mr-auto inline-block max-w-[70%] rounded-lg border border-transparent bg-slate-950/40 overflow-hidden"
      data-testid={testId}
    >
      {/* Header - Clickable to toggle */}
      <button
        type="button"
        aria-expanded={hasContent ? isOpen : undefined}
        onClick={() => {
          if (!hasContent) return;
          setIsOpen(!isOpen);
        }}
        disabled={!hasContent}
        className="flex w-full min-w-0 items-center gap-2 px-3 py-1.5 text-left transition-colors hover:bg-slate-900/60"
      >
        {isInProgress ? (
          <Loader2 className="w-3 h-3 text-slate-500 animate-spin flex-shrink-0" />
        ) : (
          <Brain className="w-3 h-3 text-slate-500 flex-shrink-0" />
        )}
        <span
          className={`inline-block text-xs font-medium ${
            isInProgress ? "llm-shimmer-text-slate" : "text-slate-400"
          }`}
        >
          {headerLabel}
        </span>
        {headerDetail && (
          <>
            <span className="shrink-0 text-xs text-slate-600" aria-hidden="true">
              ·
            </span>
            <span className="min-w-0 truncate text-xs text-slate-500" title={headerDetail}>
              {headerDetail}
            </span>
          </>
        )}
      </button>

      {/* Content - Collapsible */}
      {isBodyOpen && (
        <div className="px-3 py-2 border-t border-slate-800/70 bg-slate-950/80">
          <ol aria-label="Thinking steps" className="min-w-0">
            {visibleSteps.map((step, index) => {
              const isLast = index === visibleSteps.length - 1;
              const isActive = isInProgress && isLast;
              return (
                <li
                  key={`reasoning-step-${index}`}
                  aria-current={isActive ? "step" : undefined}
                  className="flex min-w-0 gap-2.5"
                >
                  <span
                    className="flex w-3 shrink-0 flex-col items-center pt-1"
                    aria-hidden="true"
                  >
                    <span
                      className={`h-2 w-2 shrink-0 rounded-full ${
                        isActive
                          ? "animate-pulse bg-slate-300 ring-2 ring-slate-700/80"
                          : "bg-slate-600 ring-1 ring-slate-700"
                      }`}
                    />
                    {!isLast && (
                      <span className="mt-1 min-h-3 w-px flex-1 bg-slate-700/80" />
                    )}
                  </span>
                  <p
                    className={`min-w-0 whitespace-pre-line text-xs leading-relaxed text-slate-400 sm:text-sm ${
                      isLast ? "pb-0" : "pb-3"
                    }`}
                  >
                    {step}
                  </p>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </div>
  );
}
