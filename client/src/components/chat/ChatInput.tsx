/**
 * Chat composer input and send/stop control.
 *
 * Responsibility: render the mode controls, editable message draft, and the
 * context-sensitive composer action without owning chat orchestration.
 */
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import type { KeyboardEvent, TextareaHTMLAttributes } from "react";
import { Loader2, ArrowUp, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { ChatMode, ChatPrimaryMode } from "./types";
import ModeSwitcher, { PlanToggle } from "./ModeSwitcher";

export interface ChatInputProps {
  value: string;
  onChange: (value: string) => void;
  onSend: (message: string) => void | Promise<void>;
  onStop?: () => void | Promise<void>;
  mode: ChatMode;
  disabled?: boolean;
  submissionDisabled?: boolean;
  placeholder?: string;
  maxLength?: number;
  isSending?: boolean;
  isRunning?: boolean;
  isStopping?: boolean;
  autoFocus?: boolean;
  statusMessage?: string;
  /**
   * Phase 6: composite mode selection state. ``primaryMode`` drives
   * the dropdown; ``planMode`` drives the adjacent boolean toggle.
   * Plan is disabled when ``primaryMode === 'chat'`` because the UX
   * contract makes the two mutually exclusive.
   */
  primaryMode?: ChatPrimaryMode;
  planMode?: boolean;
  onPrimaryModeChange?: (mode: ChatPrimaryMode) => void;
  onPlanModeChange?: (plan: boolean) => void;
  textAreaProps?: Omit<
    TextareaHTMLAttributes<HTMLTextAreaElement>,
    "value" | "onChange" | "onKeyDown" | "disabled" | "placeholder"
  >;
}

const MIN_ROWS = 1;
const MAX_ROWS = 8;

export function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  mode,
  disabled = false,
  submissionDisabled = false,
  placeholder,
  maxLength,
  isSending = false,
  isRunning = false,
  isStopping = false,
  autoFocus = false,
  statusMessage,
  primaryMode,
  planMode,
  onPrimaryModeChange,
  onPlanModeChange,
  textAreaProps,
}: ChatInputProps) {
  const textAreaRef = useRef<HTMLTextAreaElement | null>(null);
  const inputId = useId();
  const [isComposing, setIsComposing] = useState(false);

  const effectivePlaceholder = placeholder ?? mode.inputPlaceholder;
  const hasDraft = value.trim().length > 0;
  const showStopControl = isRunning && !hasDraft;
  const canSend =
    mode.canSendMessages && !disabled && !submissionDisabled && !isSending;
  const canStop = Boolean(isRunning && onStop && !isStopping);

  const currentLength = value.length;
  const remaining = useMemo(() => {
    if (typeof maxLength !== "number") return null;
    return Math.max(maxLength - currentLength, 0);
  }, [currentLength, maxLength]);

  const resizeTextarea = useCallback(() => {
    const element = textAreaRef.current;
    if (!element) return;

    element.style.height = "auto";
    const lineHeight = parseInt(
      window.getComputedStyle(element).lineHeight || "20",
      10,
    );
    const maxHeight = lineHeight * MAX_ROWS;
    const next = Math.min(element.scrollHeight, maxHeight);
    element.style.height = `${next}px`;
  }, []);

  useEffect(() => {
    resizeTextarea();
  }, [value, resizeTextarea]);

  const handleChange = useCallback(
    (event: React.ChangeEvent<HTMLTextAreaElement>) => {
      const nextValue = maxLength
        ? event.target.value.slice(0, maxLength)
        : event.target.value;
      onChange(nextValue);
    },
    [onChange, maxLength],
  );

  const restoreFocus = useCallback(() => {
    requestAnimationFrame(() => {
      const element = textAreaRef.current;
      if (!element) return;
      element.focus({ preventScroll: true });
      const length = element.value.length;
      element.setSelectionRange(length, length);
    });
  }, []);

  const commitSend = useCallback(() => {
    if (!canSend) return;
    const trimmed = value.trim();
    if (!trimmed) return;

    const result = onSend(trimmed);
    if (result instanceof Promise) {
      result
        .catch(() => undefined)
        .finally(() => {
          restoreFocus();
        });
    } else {
      restoreFocus();
    }
  }, [canSend, onSend, restoreFocus, value]);

  const commitStop = useCallback(() => {
    if (!canStop || !onStop) return;

    const result = onStop();
    if (result instanceof Promise) {
      result
        .catch(() => undefined)
        .finally(() => {
          restoreFocus();
        });
    } else {
      restoreFocus();
    }
  }, [canStop, onStop, restoreFocus]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (isComposing) return;
      if (event.key !== "Enter") return;

      const modifier = event.shiftKey || event.altKey || event.metaKey || event.ctrlKey;
      if (modifier) return;

      event.preventDefault();
      commitSend();
    },
    [commitSend, isComposing],
  );

  const handleCompositionStart = useCallback(() => setIsComposing(true), []);
  const handleCompositionEnd = useCallback(() => setIsComposing(false), []);

  // Phase 6: the primary-mode dropdown and the Plan overlay toggle
  // render as two independent controls side-by-side. Plan is disabled
  // (and forced off by the parent on primary-mode change) when the
  // primary mode is ``chat`` — the two are mutually exclusive per the
  // UX contract, and the backend enforces the same invariant.
  const modeIndicator =
    primaryMode && onPrimaryModeChange && onPlanModeChange ? (
      <div className="flex items-center gap-2">
        <ModeSwitcher
          primaryMode={primaryMode}
          onPrimaryModeChange={onPrimaryModeChange}
          disabled={disabled}
        />
        <PlanToggle
          planMode={Boolean(planMode)}
          onPlanModeChange={onPlanModeChange}
          disabled={disabled || primaryMode === "chat"}
        />
      </div>
    ) : (
      <span />
    );

  const { className: textAreaClassName, ...restTextAreaProps } = textAreaProps ?? {};

  return (
    <section
      aria-label="Chat input"
      className="flex flex-col gap-2 border-t border-slate-800 bg-slate-950/70 px-4 py-3"
    >
      <header className="flex items-center justify-between gap-3 text-xs text-slate-500">
        {modeIndicator}
        {typeof remaining === "number" && (
          <span className={cn("font-medium", remaining === 0 ? "text-rose-300" : "text-slate-400")}
          >
            {remaining} characters remaining
          </span>
        )}
      </header>

      {statusMessage && (
        <p role="status" className="text-xs text-indigo-300">
          {statusMessage}
        </p>
      )}

      <div className="flex items-end gap-2">
        <label htmlFor={inputId} className="sr-only">
          Chat message input
        </label>
        <div className="relative flex w-full items-end rounded-xl border border-slate-800 bg-slate-900/60 min-h-[44px]">
          <textarea
            id={inputId}
            ref={textAreaRef}
            data-testid="chat-input"
            className={cn(
              "flex-1 resize-none bg-transparent px-3 pr-12 py-3 text-sm text-slate-100 placeholder:text-slate-500",
              "focus-visible:outline-none",
              !mode.canSendMessages || disabled ? "opacity-60" : "opacity-100",
              textAreaClassName,
            )}
            value={value}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onCompositionStart={handleCompositionStart}
            onCompositionEnd={handleCompositionEnd}
            placeholder={effectivePlaceholder}
            disabled={!mode.canSendMessages || disabled}
            rows={MIN_ROWS}
            spellCheck
            autoFocus={autoFocus}
            aria-disabled={!mode.canSendMessages || disabled}
            aria-label="Chat input"
            {...restTextAreaProps}
          />
          <Button
            type="button"
            onClick={showStopControl ? commitStop : commitSend}
            disabled={showStopControl ? !canStop : !canSend || !hasDraft}
            size="icon"
            data-testid={showStopControl ? "chat-stop" : "chat-send"}
            aria-label={showStopControl ? "Stop generation" : "Send message"}
            className={cn(
              "absolute bottom-2 right-2 h-8 w-8 rounded-full flex-shrink-0",
              "bg-slate-700 text-slate-300 shadow-sm",
              "hover:bg-slate-600 hover:text-white focus-visible:ring-2 focus-visible:ring-slate-500",
              "disabled:opacity-40 disabled:cursor-not-allowed",
              canSend && hasDraft && "bg-indigo-600 text-white hover:bg-indigo-500"
            )}
          >
            {isStopping || isSending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : showStopControl ? (
              <Square className="h-3.5 w-3.5 fill-current" aria-hidden="true" />
            ) : (
              <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            <span className="sr-only">{showStopControl ? "Stop" : "Send"}</span>
          </Button>
        </div>
      </div>
    </section>
  );
}

export default ChatInput;
