/**
 * Generic identity mark resolver for subagent-run presentation.
 *
 * Responsibilities:
 * - map declarative agent icon keys to local visual assets
 * - keep known assets behind one resolver instead of card/list/detail branches
 * - provide a quiet fallback for unknown future subagent definitions
 */
import { Circle } from "lucide-react";

import pathfinderIconUrl from "@/assets/pathfinder-icon.png";
import webweaverIconUrl from "@/assets/webweaver-icon.png";
import { cn } from "@/lib/utils";

import { resolveAgentIconKey } from "../contracts/agent-run";

interface AgentIdentityIconProps {
  agentId: string;
  displayName?: string | null;
  iconKey?: string | null;
  className?: string;
  "aria-hidden"?: boolean | "true" | "false";
}

const IMAGE_ICON_SOURCES: Record<string, string> = {
  pathfinder: pathfinderIconUrl,
  webweaver: webweaverIconUrl,
};

export function AgentIdentityIcon({
  agentId,
  displayName,
  iconKey,
  className,
  "aria-hidden": ariaHidden = true,
}: AgentIdentityIconProps) {
  const resolvedIconKey = resolveAgentIconKey(agentId, iconKey);
  const imageSrc = IMAGE_ICON_SOURCES[resolvedIconKey];
  if (imageSrc) {
    return (
      <img
        src={imageSrc}
        alt=""
        draggable={false}
        className={className}
        aria-hidden={ariaHidden}
        data-agent-id={agentId}
        data-agent-icon-key={resolvedIconKey}
      />
    );
  }

  return (
    <Circle
      className={cn("text-slate-500", className)}
      aria-hidden={ariaHidden}
      data-agent-id={agentId}
      data-agent-icon-key={resolvedIconKey}
      data-agent-display-name={displayName ?? undefined}
    />
  );
}

export default AgentIdentityIcon;
