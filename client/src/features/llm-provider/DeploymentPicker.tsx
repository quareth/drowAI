/**
 * Catalog-driven workload deployment selector for approved LLM deployments.
 *
 * The picker only renders deployment references supplied by the backend
 * catalog and uses backend metadata for lifecycle, runnability, and pricing.
 */
import { useMemo, useState } from "react";
import { CheckCircle, Search } from "lucide-react";

import {
  formatPricingStatus,
  getDeploymentCandidateGroups,
  getSingleEligibleDeployment,
  isDeploymentCandidateSelectable,
  sameDeploymentRef,
} from "@/features/llm-provider/catalog";
import type {
  LLMDeploymentRef,
  LLMDeploymentStatusOverride,
  LLMModelCatalogResponse,
} from "@/features/llm-provider/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export interface DeploymentPickerProps {
  catalog: LLMModelCatalogResponse | null | undefined;
  selectedDeploymentRef?: LLMDeploymentRef | null;
  statusOverrides?: LLMDeploymentStatusOverride[];
  onSelectDeployment: (deploymentRef: LLMDeploymentRef) => void;
  isPending?: boolean;
}

export function DeploymentPicker({
  catalog,
  selectedDeploymentRef,
  statusOverrides = [],
  onSelectDeployment,
  isPending = false,
}: DeploymentPickerProps) {
  const [searchTerm, setSearchTerm] = useState("");
  const candidateGroups = useMemo(
    () => getDeploymentCandidateGroups(catalog, statusOverrides),
    [catalog, statusOverrides],
  );
  const candidates = useMemo(
    () => candidateGroups.flatMap((group) => group.candidates),
    [candidateGroups],
  );
  const defaultCandidate = useMemo(
    () => getSingleEligibleDeployment(candidates),
    [candidates],
  );
  const normalizedSearch = searchTerm.trim().toLowerCase();
  const filteredGroups = useMemo(() => {
    if (!normalizedSearch) {
      return candidateGroups;
    }
    return candidateGroups
      .map((group) => ({
        ...group,
        candidates: group.candidates.filter((candidate) =>
          [
            group.modelLabel,
            group.canonicalModelId,
            candidate.deploymentLabel,
            candidate.providerLabel,
            candidate.modelLabel,
            candidate.modelId,
            candidate.canonicalModelId,
            candidate.exactWireModelId,
            candidate.apiSurface,
          ]
            .filter(Boolean)
            .some((value) => String(value).toLowerCase().includes(normalizedSearch)),
        ),
      }))
      .filter((group) => group.candidates.length > 0);
  }, [candidateGroups, normalizedSearch]);

  return (
    <div className="space-y-4">
      <div className="relative">
        <Input
          type="search"
          placeholder="Search deployments"
          value={searchTerm}
          onChange={(event) => setSearchTerm(event.target.value)}
          className="border-slate-700 bg-slate-950 pl-9 text-white"
        />
        <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
      </div>

      {filteredGroups.length === 0 ? (
        <p className="text-sm text-slate-400">No backend-provided deployments match.</p>
      ) : (
        <div className="space-y-5">
          {filteredGroups.map((group) => (
            <section key={group.key} className="space-y-2">
              <h4 className="text-xs font-semibold uppercase text-slate-400">
                {group.modelLabel}
              </h4>
              <div className="space-y-2">
                {group.candidates.map((candidate) => {
                  const selectable = isDeploymentCandidateSelectable(candidate);
                  const selected = sameDeploymentRef(
                    candidate.deploymentRef,
                    selectedDeploymentRef,
                  );
                  const defaulted =
                    !selectedDeploymentRef &&
                    sameDeploymentRef(candidate.deploymentRef, defaultCandidate?.deploymentRef);

                  return (
                    <div
                      key={candidate.deploymentRef.deployment_id}
                      className="rounded-md border border-slate-800 bg-slate-950 p-3"
                    >
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0 space-y-2">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="text-sm font-medium text-white">
                              {candidate.deploymentLabel}
                            </p>
                            {candidate.deploymentLabel !== candidate.providerLabel ? (
                              <Badge className="bg-slate-800 text-slate-200">
                                {candidate.providerLabel}
                              </Badge>
                            ) : null}
                            {selected || defaulted ? (
                              <Badge className="bg-emerald-900/70 text-emerald-100">
                                <CheckCircle className="mr-1 h-3 w-3" />
                                Current/default
                              </Badge>
                            ) : null}
                            <Badge className="bg-slate-800 text-slate-200">
                              Lifecycle: {candidate.lifecycleState}
                            </Badge>
                            <Badge className="bg-slate-800 text-slate-200">
                              Runnability: {candidate.status}
                            </Badge>
                          </div>
                          <div className="grid gap-x-4 gap-y-1 text-xs text-slate-400 sm:grid-cols-2">
                            <p>Context: {candidate.contextWindowTokens} tokens</p>
                            <p>Output: {candidate.maxOutputTokens} tokens</p>
                            <p>Pricing: {formatPricingStatus(candidate.pricingStatus)}</p>
                          </div>
                          {candidate.reason ? (
                            <p className="text-xs text-amber-200">{candidate.reason}</p>
                          ) : null}
                        </div>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={!selectable || isPending}
                          onClick={() => onSelectDeployment(candidate.deploymentRef)}
                          aria-label={deploymentSelectLabel(
                            group.modelLabel,
                            candidate.deploymentLabel,
                            candidate.providerLabel,
                          )}
                          className="border-slate-600 text-slate-200 hover:text-white"
                        >
                          Select {group.modelLabel} on {candidate.deploymentLabel}
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

function deploymentSelectLabel(
  modelLabel: string,
  deploymentLabel: string,
  providerLabel: string,
): string {
  if (deploymentLabel === providerLabel) {
    return `Select ${modelLabel} on ${deploymentLabel}`;
  }
  return `Select ${modelLabel} on ${deploymentLabel} (${providerLabel})`;
}

export default DeploymentPicker;
