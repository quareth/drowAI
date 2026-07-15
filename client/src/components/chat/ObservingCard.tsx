import { Eye, Loader2 } from "lucide-react";

import { useCardToggleState } from "@/hooks/useCardToggleState";

interface ObservingCardProps {
  observation: string;
  defaultOpen?: boolean;
  isInProgress?: boolean;
  hasContent?: boolean;
  /** Stable identifier so collapse/expand state persists across remounts. */
  stateKey?: string;
  /** Stable selector for deterministic E2E assertions. */
  testId?: string;
}

/**
 * Displays observation text emitted when the agent summarizes tool output.
 */
export function ObservingCard({
  observation,
  defaultOpen = false,
  isInProgress = false,
  hasContent = false,
  stateKey,
  testId,
}: ObservingCardProps) {
  const [isOpen, setIsOpen] = useCardToggleState(stateKey, defaultOpen);
  const isBodyOpen = hasContent && isOpen;

  return (
    <div
      data-testid={testId}
      className="mb-1 mr-auto inline-block max-w-[70%] rounded-lg border border-transparent bg-emerald-950/40 overflow-hidden"
    >
      <button
        onClick={() => {
          if (!hasContent) return;
          setIsOpen(!isOpen);
        }}
        disabled={!hasContent}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-emerald-900/60 transition-colors"
      >
        {isInProgress ? (
          <Loader2 className="w-3 h-3 text-emerald-400 animate-spin flex-shrink-0" />
        ) : (
          <Eye className="w-3 h-3 text-emerald-400 flex-shrink-0" />
        )}
        <span
          className={`inline-block text-xs font-medium ${
            isInProgress ? "llm-shimmer-text-emerald" : "text-emerald-200/80"
          }`}
        >
          {isInProgress ? "Observing" : "Observation"}
        </span>
      </button>

      {isBodyOpen && (
        <div className="px-3 py-2 border-t border-emerald-900/70 bg-emerald-950/80">
          <p className="text-xs sm:text-sm text-emerald-100/80 leading-relaxed whitespace-pre-line">
            {observation}
          </p>
        </div>
      )}
    </div>
  );
}


