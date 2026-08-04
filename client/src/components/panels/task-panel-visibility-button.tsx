/**
 * Shared control for opening and closing the Overview task panel.
 */
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface TaskPanelVisibilityButtonProps {
  isCollapsed: boolean;
  onToggle: () => void;
  className?: string;
}

export function TaskPanelVisibilityButton({
  isCollapsed,
  onToggle,
  className,
}: TaskPanelVisibilityButtonProps) {
  const Icon = isCollapsed ? PanelLeftOpen : PanelLeftClose;
  const label = isCollapsed ? "Open task panel" : "Close task panel";

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className={cn("h-7 w-7 shrink-0 p-0 text-slate-400", className)}
      onClick={onToggle}
      aria-label={label}
      title={label}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
    </Button>
  );
}
