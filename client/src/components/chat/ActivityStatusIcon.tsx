/**
 * Shared static-or-working icon used by streamed chat activity headers.
 */
import type { ElementType } from "react";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

interface ActivityStatusIconProps {
  isInProgress: boolean;
  icon: ElementType;
  className?: string;
}

export function ActivityStatusIcon({
  isInProgress,
  icon: Icon,
  className,
}: ActivityStatusIconProps) {
  const StatusIcon = isInProgress ? Loader2 : Icon;

  return (
    <StatusIcon
      className={cn(className, isInProgress && "animate-spin")}
      aria-hidden="true"
    />
  );
}
