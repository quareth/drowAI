/* Compact chip renderer for a service embedded inside an asset topology node. */

import { cn } from "@/lib/utils";
import { formatServiceDisplayName } from "@/components/engagements/service-presentation";
import { SERVICE_CHIP_MAX_WIDTH } from "@/components/engagements/territory/topology-presentation";
import type { TopologyServiceChip } from "@/components/engagements/territory/topology-types";

function formatChipLabel(chip: TopologyServiceChip): string {
  const parts: string[] = [];
  if (chip.port !== null) {
    parts.push(`:${chip.port}`);
  }
  const displayName = formatServiceDisplayName({
    id: chip.id,
    service_key: chip.id.startsWith("service.socket:") ? chip.id : null,
    service_name: chip.label && !chip.label.startsWith("service.socket:") ? chip.label : null,
    protocol: chip.protocol,
    port: chip.port,
  });
  const socketLabel =
    chip.protocol && chip.port !== null ? `${chip.protocol.toUpperCase()} ${chip.port}` : null;
  if (displayName && displayName !== chip.id && !displayName.startsWith("service.socket:")) {
    parts.push(displayName === socketLabel && chip.protocol ? chip.protocol.toUpperCase() : displayName);
  }
  if (parts.length === 0) {
    return chip.id;
  }
  return parts.join(" ");
}

interface ServiceChipProps {
  chip: TopologyServiceChip;
  active?: boolean;
  onClick: (chip: TopologyServiceChip) => void;
}

export function ServiceChip({ chip, active = false, onClick }: ServiceChipProps) {
  return (
    <button
      type="button"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClick(chip);
      }}
      title={`${chip.protocol ?? ""}/${chip.label} (${chip.status ?? "unknown"})`}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium leading-tight",
        "border-slate-500/35 bg-slate-800/35 text-slate-300 transition-colors hover:border-slate-400/50 hover:text-slate-100 focus:outline-none focus:ring-1 focus:ring-slate-400/60",
        active && "ring-1 ring-emerald-400/70",
      )}
      style={{ maxWidth: SERVICE_CHIP_MAX_WIDTH }}
      data-testid={`service-chip-${chip.id}`}
    >
      <span className="truncate">{formatChipLabel(chip)}</span>
    </button>
  );
}
