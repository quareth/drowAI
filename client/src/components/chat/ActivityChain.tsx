/**
 * Shared wrapper for connected activity timelines.
 *
 * Responsibility:
 * - render main-turn and subagent activity groups with the same visual chain
 * - avoid changing the underlying reasoning/tool/observation/message renderers
 */
import { Children, type ReactNode } from "react";

import { cn } from "@/lib/utils";

interface ActivityChainProps {
  children: ReactNode;
  activeIndexes?: Set<number>;
  className?: string;
  testId?: string;
}

export function ActivityChain({
  children,
  activeIndexes,
  className,
  testId = "activity-chain",
}: ActivityChainProps) {
  const items = Children.toArray(children);

  return (
    <div
      className={cn(
        "flex min-w-0 flex-col",
        className,
      )}
      data-testid={testId}
      data-activity-chain="true"
    >
      {items.map((child, index) => {
        const isLast = index === items.length - 1;
        const isActive = activeIndexes?.has(index) ?? false;
        return (
          <div
            key={`activity-chain-item-${index}`}
            className="flex min-w-0 gap-2.5"
            data-testid={`${testId}-item-${index}`}
            data-chain-active={isActive ? "true" : "false"}
          >
            <span
              className="flex w-3 shrink-0 flex-col items-center pt-2"
              aria-hidden="true"
            >
              <span
                className={cn(
                  "h-2 w-2 shrink-0 rounded-full",
                  isActive
                    ? "animate-pulse bg-slate-300 ring-2 ring-slate-700/80"
                    : "bg-slate-600 ring-1 ring-slate-700",
                )}
                data-testid={`${testId}-node-${index}`}
              />
              {!isLast && (
                <span
                  className="mt-1 min-h-3 w-px flex-1 bg-slate-700/80"
                  data-testid={`${testId}-connector-${index}`}
                />
              )}
            </span>
            <div className="min-w-0 flex-1">{child}</div>
          </div>
        );
      })}
    </div>
  );
}

export default ActivityChain;
