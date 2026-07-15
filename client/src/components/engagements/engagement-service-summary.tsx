/* Shared linked-service summary row for engagement detail panels. */

import {
  engagementDetailSectionClass,
} from "@/components/engagements/engagement-ui";
import {
  formatServiceDisplayName,
  formatServiceIdentityLabel,
  formatServiceSocket,
} from "@/components/engagements/service-presentation";
import type { ServicePresentationInput } from "@/components/engagements/service-presentation";
import type { RichServiceState } from "@/types/knowledge";
import { formatServiceFingerprint, getMetadataState } from "@/types/knowledge";

interface EngagementServiceSummaryInput extends ServicePresentationInput {
  product?: string | null;
  version?: string | null;
}

interface EngagementServiceSummaryProps {
  service: EngagementServiceSummaryInput;
}

export function EngagementServiceSummary({ service }: EngagementServiceSummaryProps) {
  const richState = getMetadataState<RichServiceState>(
    service.metadata as Record<string, unknown> | undefined,
  );
  const serviceDisplayName = formatServiceDisplayName(service);
  const serviceIdentityLabel = formatServiceIdentityLabel(service);
  const serviceSocket = formatServiceSocket(service);
  const fingerprint = formatServiceFingerprint({
    product: service.product,
    version: service.version,
    versionRaw: richState?.version_raw,
    versionRelation: richState?.version_relation,
  });

  return (
    <div className={`${engagementDetailSectionClass} flex items-start justify-between gap-3`}>
      <span className="min-w-0 text-slate-200">
        <span>{serviceDisplayName}</span>
        {serviceIdentityLabel && (
          <span className="block max-w-56 truncate text-slate-500 mt-0.5" title={serviceIdentityLabel}>
            {serviceIdentityLabel}
          </span>
        )}
        {fingerprint && <span className="block text-slate-400 mt-0.5">{fingerprint}</span>}
        {richState?.http_title && (
          <span className="block text-slate-400 mt-0.5" title="HTTP title">{richState.http_title}</span>
        )}
        {richState?.server_header && !fingerprint?.includes(richState.server_header) && (
          <span className="block text-slate-500 mt-0.5" title="Server header">
            Server: {richState.server_header}
          </span>
        )}
      </span>
      <span className="shrink-0 text-slate-400">
        {serviceSocket || "-"}
      </span>
    </div>
  );
}
