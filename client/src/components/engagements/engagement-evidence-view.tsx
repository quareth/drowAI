/* Evidence workspace view for inline engagement-scoped evidence browsing. */

import { EngagementEvidenceCatalog } from "@/components/engagements/engagement-evidence-catalog";
import type {
  EvidenceFilters,
  EvidenceListItem,
} from "@/types/engagement-knowledge";

interface EngagementEvidenceViewProps {
  evidence: EvidenceListItem[];
  filters: EvidenceFilters;
  onFiltersChange: (filters: EvidenceFilters) => void;
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string | null;
  emptyMessage?: string;
  onPreviewEvidence: (evidenceId: string) => void;
}

export function EngagementEvidenceView({
  evidence,
  filters,
  onFiltersChange,
  isLoading = false,
  isError = false,
  errorMessage = null,
  emptyMessage,
  onPreviewEvidence,
}: EngagementEvidenceViewProps) {
  return (
    <EngagementEvidenceCatalog
      evidence={evidence}
      filters={filters}
      onFiltersChange={onFiltersChange}
      isLoading={isLoading}
      isError={isError}
      errorMessage={errorMessage}
      emptyMessage={emptyMessage}
      onPreviewEvidence={onPreviewEvidence}
    />
  );
}
