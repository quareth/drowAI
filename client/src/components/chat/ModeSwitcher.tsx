/**
 * Primary-mode dropdown and Plan overlay toggle.
 *
 * Phase 6: Plan is rendered as a separate adjacent boolean toggle, not
 * a dropdown option. The dropdown exposes only the three primary
 * execution tiers (chat / agent / agent_full). Chat is mutually
 * exclusive with Plan: selecting Chat disables and clears the
 * toggle. The backend enforces the same invariant.
 */
import React, { useMemo } from "react";
import type { JSX } from "react";
import { Check, MessageCircle, Bot, Rocket, ClipboardList } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ChatPrimaryMode } from "./types";

interface ModeOption {
  key: ChatPrimaryMode;
  label: string;
  description: string;
  icon: JSX.Element;
}

const MODE_OPTIONS: ModeOption[] = [
  {
    key: "chat",
    label: "Chat",
    description: "Chat with the AI assistant",
    icon: <MessageCircle className="h-4 w-4" aria-hidden="true" />,
  },
  {
    key: "agent",
    label: "Agent",
    description: "Asks before taking actions",
    icon: <Bot className="h-4 w-4" aria-hidden="true" />,
  },
  {
    key: "agent_full",
    label: "Agent (Full Access)",
    description: "Takes actions automatically",
    icon: <Rocket className="h-4 w-4" aria-hidden="true" />,
  },
];

interface ModeSwitcherProps {
  primaryMode: ChatPrimaryMode;
  onPrimaryModeChange: (mode: ChatPrimaryMode) => void;
  disabled?: boolean;
  className?: string;
}

export function ModeSwitcher({
  primaryMode,
  onPrimaryModeChange,
  disabled,
  className,
}: ModeSwitcherProps) {
  const activeOption = useMemo(() => {
    return MODE_OPTIONS.find((option) => option.key === primaryMode) ?? MODE_OPTIONS[1];
  }, [primaryMode]);

  const handleSelect = (option: ModeOption) => {
    if (disabled) return;
    if (option.key !== primaryMode) {
      onPrimaryModeChange(option.key);
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled}
          data-testid="chat-mode-switcher"
          className={cn(
            "h-7 rounded-full border-slate-700 bg-slate-900/70 px-3 text-xs font-medium text-slate-200",
            "hover:bg-slate-900 hover:text-white",
            className,
          )}
        >
          {activeOption.label}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-48">
        <DropdownMenuLabel className="text-[10px] uppercase">Switch mode</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {MODE_OPTIONS.map((option) => {
          const isActive = option.key === activeOption.key;

          return (
            <DropdownMenuItem
              key={option.key}
              disabled={disabled}
              onClick={() => handleSelect(option)}
              data-testid={`chat-mode-option-${option.key}`}
              className="flex items-center gap-2 text-xs"
            >
              <span
                className={cn(
                  "flex h-5 w-5 items-center justify-center rounded-full bg-slate-800",
                  isActive && "bg-emerald-600 text-white",
                )}
                aria-hidden="true"
              >
                {React.cloneElement(option.icon, { className: "h-3 w-3" })}
              </span>
              <div className="flex-1">
                <p className="font-medium leading-tight text-xs">{option.label}</p>
                <p className="text-[10px] text-slate-500 leading-tight">{option.description}</p>
              </div>
              {isActive && <Check className="h-3 w-3 text-emerald-400" aria-hidden="true" />}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

interface PlanToggleProps {
  planMode: boolean;
  onPlanModeChange: (next: boolean) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * Adjacent on/off toggle for the Plan route overlay.
 *
 * Plan is disabled (and forced off by the parent) when the primary
 * mode is ``chat``. The toggle renders as a small pill button so it
 * sits beside the primary-mode dropdown without competing for
 * visual weight.
 */
export function PlanToggle({
  planMode,
  onPlanModeChange,
  disabled,
  className,
}: PlanToggleProps) {
  const handleClick = () => {
    if (disabled) return;
    onPlanModeChange(!planMode);
  };
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      disabled={disabled}
      onClick={handleClick}
      aria-pressed={planMode}
      data-testid="chat-plan-toggle"
      className={cn(
        "h-7 rounded-full border-slate-700 bg-slate-900/70 px-3 text-xs font-medium",
        planMode
          ? "border-emerald-600 bg-emerald-600/20 text-emerald-200 hover:bg-emerald-600/30"
          : "text-slate-300 hover:bg-slate-900 hover:text-white",
        disabled && "opacity-50 cursor-not-allowed",
        className,
      )}
    >
      <ClipboardList className="mr-1 h-3 w-3" aria-hidden="true" />
      Plan
    </Button>
  );
}

export default ModeSwitcher;
