/* User-scoped knowledge query hooks and React Query key builders. */

import { useCallback, useState } from "react";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api-config";
import type {
  AssetDetail,
  AssetListItem,
  AssetsFilters,
  KnowledgeGraphSnapshot,
  KnowledgeSummary,
  EvidenceFilters,
  EvidenceListItem,
  FindingDetail,
  FindingListItem,
  FindingsFilters,
  PaginatedResponse,
} from "@/types/knowledge";

type QueryParamValue = string | number | boolean | undefined | null;
type QueryParamRecord = Record<string, QueryParamValue>;

function normalizeFilterRecord<T extends object>(filters: T | undefined): QueryParamRecord {
  if (!filters) {
    return {};
  }
  const entries = Object.entries(filters as QueryParamRecord)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .sort(([left], [right]) => left.localeCompare(right));
  return Object.fromEntries(entries) as QueryParamRecord;
}

function toQueryString(filters?: QueryParamRecord): string {
  if (!filters) {
    return "";
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(normalizeFilterRecord(filters))) {
    params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

async function fetchJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch(endpoint, { method: "GET", signal });
  if (!response.ok) {
    const details = await response.text().catch(() => "");
    throw new Error(`${response.status}: ${details || response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export const knowledgeKeys = {
  summary: () => ["knowledge", "summary"] as const,
  findings: (filters?: FindingsFilters) =>
    ["knowledge", "findings", normalizeFilterRecord(filters)] as const,
  finding: (findingId: string) => ["knowledge", "finding", findingId] as const,
  assets: (filters?: AssetsFilters) =>
    ["knowledge", "assets", normalizeFilterRecord(filters)] as const,
  asset: (assetId: string) => ["knowledge", "asset", assetId] as const,
  services: (params?: { limit?: number; offset?: number }) =>
    ["knowledge", "services", normalizeFilterRecord(params)] as const,
  evidence: (filters?: EvidenceFilters) =>
    ["knowledge", "evidence", normalizeFilterRecord(filters)] as const,
  graph: () => ["knowledge", "graph"] as const,
};

export async function invalidateKnowledgeQueries(queryClient: QueryClient): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ["knowledge"] });
}

export function useKnowledgeRefresh() {
  const queryClient = useQueryClient();
  const [isRefreshing, setIsRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setIsRefreshing(true);
    try {
      await invalidateKnowledgeQueries(queryClient);
    } finally {
      setIsRefreshing(false);
    }
  }, [queryClient]);

  return { refresh, isRefreshing };
}

export function useKnowledgeSummary() {
  return useQuery<KnowledgeSummary>({
    queryKey: knowledgeKeys.summary(),
    queryFn: ({ signal }) => fetchJson<KnowledgeSummary>("/api/knowledge/summary", signal),
  });
}

export function useKnowledgeFindings(filters?: FindingsFilters) {
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<PaginatedResponse<FindingListItem>>({
    queryKey: knowledgeKeys.findings(normalizedFilters),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<FindingListItem>>(
        `/api/knowledge/findings${toQueryString(normalizedFilters)}`,
        signal,
      ),
  });
}

export function useKnowledgeFinding(findingId: string | null | undefined) {
  const id = findingId?.trim() || null;
  return useQuery<FindingDetail>({
    queryKey: id ? knowledgeKeys.finding(id) : ["knowledge", "finding", "__disabled__"],
    enabled: Boolean(id),
    queryFn: ({ signal }) => {
      if (!id) {
        throw new Error("Finding id is required.");
      }
      return fetchJson<FindingDetail>(
        `/api/knowledge/findings/${encodeURIComponent(id)}`,
        signal,
      );
    },
  });
}

export function useKnowledgeAssets(filters?: AssetsFilters) {
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<PaginatedResponse<AssetListItem>>({
    queryKey: knowledgeKeys.assets(normalizedFilters),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<AssetListItem>>(
        `/api/knowledge/assets${toQueryString(normalizedFilters)}`,
        signal,
      ),
  });
}

export function useKnowledgeAsset(assetId: string | null | undefined) {
  const id = assetId?.trim() || null;
  return useQuery<AssetDetail>({
    queryKey: id ? knowledgeKeys.asset(id) : ["knowledge", "asset", "__disabled__"],
    enabled: Boolean(id),
    queryFn: ({ signal }) => fetchJson<AssetDetail>(`/api/knowledge/assets/${id}`, signal),
  });
}

export function useKnowledgeServices(params?: { limit?: number; offset?: number }) {
  const normalizedParams = normalizeFilterRecord(params);
  return useQuery<PaginatedResponse<Record<string, unknown>>>({
    queryKey: knowledgeKeys.services(normalizedParams),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<Record<string, unknown>>>(
        `/api/knowledge/services${toQueryString(normalizedParams)}`,
        signal,
      ),
  });
}

export function useKnowledgeEvidence(filters?: EvidenceFilters) {
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<PaginatedResponse<EvidenceListItem>>({
    queryKey: knowledgeKeys.evidence(normalizedFilters),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<EvidenceListItem>>(
        `/api/knowledge/evidence${toQueryString(normalizedFilters)}`,
        signal,
      ),
  });
}

export function useKnowledgeGraph() {
  return useQuery<KnowledgeGraphSnapshot>({
    queryKey: knowledgeKeys.graph(),
    queryFn: ({ signal }) =>
      fetchJson<KnowledgeGraphSnapshot>("/api/knowledge/relationships/graph", signal),
  });
}
