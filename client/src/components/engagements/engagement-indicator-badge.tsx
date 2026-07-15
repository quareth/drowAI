/* Reusable compact badge for engagement workspace indicators. */

import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import {
  engagementIndicatorSizeClass,
  engagementIndicatorToneClass,
  type EngagementIndicatorSize,
  type EngagementIndicatorTone,
} from "@/components/engagements/engagement-indicator-presentation";
import { cn } from "@/lib/utils";

interface EngagementIndicatorBadgeProps
  extends Omit<ComponentPropsWithoutRef<typeof Badge>, "children" | "variant"> {
  children: ReactNode;
  label?: string;
  size?: EngagementIndicatorSize;
  tone?: EngagementIndicatorTone;
}

export function EngagementIndicatorBadge({
  children,
  className,
  label,
  size = "md",
  tone = "neutral",
  ...rest
}: EngagementIndicatorBadgeProps) {
  const { "aria-label": ariaLabel, ...badgeProps } = rest;

  return (
    <Badge
      {...badgeProps}
      variant="outline"
      className={cn(
        engagementIndicatorToneClass(tone),
        engagementIndicatorSizeClass(size),
        className,
      )}
      aria-label={label ?? ariaLabel}
    >
      {children}
    </Badge>
  );
}
