/* Shared engagement-knowledge query hooks and stable React Query key builders. */

import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api-config";
import { apiRequest } from "@/lib/queryClient";
import type {
  AssetDetail,
  AssetListItem,
  AssetsFilters,
  EngagementGraphSnapshot,
  EngagementListItem,
  EngagementSummary,
  EvidenceFilters,
  EvidenceListItem,
  FindingDetail,
  FindingListItem,
  FindingsFilters,
  PaginatedResponse,
  WebSurfaceOriginSummary,
  WebSurfacePathPage,
} from "@/types/engagement-knowledge";

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

async function parseMutationJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const details = await response.text().catch(() => "");
    throw new Error(`${response.status}: ${details || response.statusText}`);
  }
  return response.json() as Promise<T>;
}

function normalizeEngagementId(
  engagementId: string | number | null | undefined,
): string | null {
  if (engagementId === null || engagementId === undefined) {
    return null;
  }
  const trimmed = String(engagementId).trim();
  return trimmed.length > 0 ? trimmed : null;
}

export const engagementKnowledgeKeys = {
  engagements: (filters?: { query?: string; status?: string; limit?: number; offset?: number }) => {
    const normalizedFilters = normalizeFilterRecord(filters);
    if (Object.keys(normalizedFilters).length === 0) {
      return ["engagements"] as const;
    }
    return ["engagements", normalizedFilters] as const;
  },
  engagement: (engagementId: string) => ["engagement", engagementId] as const,
  summary: (engagementId: string) => ["engagement", engagementId, "summary"] as const,
  findings: (engagementId: string, filters?: FindingsFilters) =>
    ["engagement", engagementId, "findings", normalizeFilterRecord(filters)] as const,
  assets: (engagementId: string, filters?: AssetsFilters) =>
    ["engagement", engagementId, "assets", normalizeFilterRecord(filters)] as const,
  evidence: (engagementId: string, filters?: EvidenceFilters) =>
    ["engagement", engagementId, "evidence", normalizeFilterRecord(filters)] as const,
  graph: (engagementId: string) => ["engagement", engagementId, "graph"] as const,
  finding: (engagementId: string, findingId: string) =>
    ["engagement", engagementId, "finding", findingId] as const,
  asset: (engagementId: string, assetId: string) =>
    ["engagement", engagementId, "asset", assetId] as const,
  webSurfacePrefix: (engagementId: string) => ["engagement", engagementId, "web-surface"] as const,
  webSurfaceOrigins: (
    engagementId: string,
    serviceKey: string,
    includeNoisy: boolean | undefined = false,
  ) =>
    [
      "engagement",
      engagementId,
      "web-surface",
      "origins",
      serviceKey,
      { include_noisy: Boolean(includeNoisy) },
    ] as const,
  webSurfacePaths: (
    engagementId: string,
    serviceKey: string,
    filters?: QueryParamRecord,
  ) =>
    [
      "engagement",
      engagementId,
      "web-surface",
      "paths",
      serviceKey,
      normalizeFilterRecord(filters),
    ] as const,
};

export function getEngagementInvalidationTargets(engagementId: string) {
  return [
    engagementKnowledgeKeys.engagements(),
    engagementKnowledgeKeys.engagement(engagementId),
    engagementKnowledgeKeys.summary(engagementId),
    ["engagement", engagementId, "findings"] as const,
    ["engagement", engagementId, "assets"] as const,
    ["engagement", engagementId, "evidence"] as const,
    engagementKnowledgeKeys.graph(engagementId),
    engagementKnowledgeKeys.webSurfacePrefix(engagementId),
  ];
}

export interface WebSurfaceOriginsFilters {
  include_noisy?: boolean;
}

export interface WebSurfacePathFilters {
  origin_key?: string;
  include_noisy?: boolean;
  limit?: number;
  offset?: number;
}

function normalizeWebSurfaceOriginsResponse(
  payload: Partial<{ service_key: string; items: WebSurfaceOriginSummary[] }> | undefined,
  serviceKey: string,
): { service_key: string; items: WebSurfaceOriginSummary[] } {
  return {
    service_key: String(payload?.service_key ?? serviceKey),
    items: Array.isArray(payload?.items) ? payload.items : [],
  };
}

function normalizeWebSurfacePathPageResponse(
  payload: Partial<WebSurfacePathPage> | undefined,
  serviceKey: string,
  normalizedFilters: QueryParamRecord,
): WebSurfacePathPage {
  return {
    service_key: payload?.service_key ?? serviceKey,
    origin_key: (payload?.origin_key as string | null | undefined) ?? null,
    items: Array.isArray(payload?.items) ? payload.items : [],
    total: Number(payload?.total ?? 0),
    limit: Number(payload?.limit ?? normalizedFilters.limit ?? 100),
    offset: Number(payload?.offset ?? normalizedFilters.offset ?? 0),
    hidden_noisy: Number(payload?.hidden_noisy ?? 0),
  };
}

export async function invalidateEngagementKnowledgeQueries(
  queryClient: QueryClient,
  engagementId: string | number | null | undefined,
): Promise<void> {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  if (!normalizedEngagementId) {
    return;
  }
  const targets = getEngagementInvalidationTargets(normalizedEngagementId);
  await Promise.all(
    targets.map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}

export function useEngagementKnowledgeRefresh(
  engagementId: string | number | null | undefined,
) {
  const queryClient = useQueryClient();
  const [isRefreshing, setIsRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setIsRefreshing(true);
    try {
      await invalidateEngagementKnowledgeQueries(queryClient, engagementId);
    } finally {
      setIsRefreshing(false);
    }
  }, [engagementId, queryClient]);

  return {
    refresh,
    isRefreshing,
  };
}

export function useCreateEngagement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (data: { name: string; description?: string }) => {
      const response = await apiRequest("POST", "/api/engagements/", data);
      return parseMutationJson<{ id: number; name: string }>(response as Response);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: engagementKnowledgeKeys.engagements() });
    },
  });
}

export interface EngagementListFilters {
  query?: string;
  status?: "active" | "archived" | "all";
  limit?: number;
  offset?: number;
}

export function useArchiveEngagement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (engagementId: number) => {
      const response = await apiRequest("DELETE", `/api/engagements/${engagementId}`);
      return parseMutationJson<{ id: number; status: string }>(response as Response);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: engagementKnowledgeKeys.engagements() });
    },
  });
}

export function useRestoreEngagement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (engagementId: number) => {
      const response = await apiRequest("POST", `/api/engagements/${engagementId}/restore`);
      return parseMutationJson<{ id: number; status: string }>(response as Response);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: engagementKnowledgeKeys.engagements() });
    },
  });
}

export function useEngagements(filters?: EngagementListFilters) {
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<PaginatedResponse<EngagementListItem>>({
    queryKey: engagementKnowledgeKeys.engagements(filters),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<EngagementListItem>>(
        `/api/engagements${toQueryString(normalizedFilters)}`,
        signal,
      ),
  });
}

export function useEngagement(engagementId: string | number | null | undefined) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  return useQuery<EngagementListItem>({
    queryKey: normalizedEngagementId
      ? engagementKnowledgeKeys.engagement(normalizedEngagementId)
      : ["engagement", "__disabled__"],
    enabled: Boolean(normalizedEngagementId),
    queryFn: ({ signal }) =>
      fetchJson<EngagementListItem>(
        `/api/engagements/${normalizedEngagementId}`,
        signal,
      ),
  });
}

export function useEngagementSummary(engagementId: string | number | null | undefined) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  return useQuery<EngagementSummary>({
    queryKey: normalizedEngagementId
      ? engagementKnowledgeKeys.summary(normalizedEngagementId)
      : ["engagement", "__disabled__", "summary"],
    enabled: Boolean(normalizedEngagementId),
    queryFn: ({ signal }) =>
      fetchJson<EngagementSummary>(
        `/api/engagements/${normalizedEngagementId}/summary`,
        signal,
      ),
  });
}

export function useEngagementFindings(
  engagementId: string | number | null | undefined,
  filters?: FindingsFilters,
) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<PaginatedResponse<FindingListItem>>({
    queryKey: normalizedEngagementId
      ? engagementKnowledgeKeys.findings(normalizedEngagementId, normalizedFilters)
      : ["engagement", "__disabled__", "findings", normalizedFilters],
    enabled: Boolean(normalizedEngagementId),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<FindingListItem>>(
        `/api/engagements/${normalizedEngagementId}/findings${toQueryString(normalizedFilters)}`,
        signal,
      ),
  });
}

export function useEngagementFinding(
  engagementId: string | number | null | undefined,
  findingId: string | null | undefined,
) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  const normalizedFindingId = normalizeEngagementId(findingId);
  return useQuery<FindingDetail>({
    queryKey:
      normalizedEngagementId && normalizedFindingId
        ? engagementKnowledgeKeys.finding(normalizedEngagementId, normalizedFindingId)
        : ["engagement", "__disabled__", "finding", "__disabled__"],
    enabled: Boolean(normalizedEngagementId && normalizedFindingId),
    queryFn: ({ signal }) => {
      if (!normalizedEngagementId || !normalizedFindingId) {
        throw new Error("Engagement id and finding id are required.");
      }
      const endpoint = `/api/engagements/${encodeURIComponent(
        normalizedEngagementId,
      )}/findings/${encodeURIComponent(normalizedFindingId)}`;
      return fetchJson<FindingDetail>(
        endpoint,
        signal,
      );
    },
  });
}

export function useEngagementAssets(
  engagementId: string | number | null | undefined,
  filters?: AssetsFilters,
) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<PaginatedResponse<AssetListItem>>({
    queryKey: normalizedEngagementId
      ? engagementKnowledgeKeys.assets(normalizedEngagementId, normalizedFilters)
      : ["engagement", "__disabled__", "assets", normalizedFilters],
    enabled: Boolean(normalizedEngagementId),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<AssetListItem>>(
        `/api/engagements/${normalizedEngagementId}/assets${toQueryString(normalizedFilters)}`,
        signal,
      ),
  });
}

export function useEngagementAsset(
  engagementId: string | number | null | undefined,
  assetId: string | null | undefined,
) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  const normalizedAssetId = normalizeEngagementId(assetId);
  return useQuery<AssetDetail>({
    queryKey:
      normalizedEngagementId && normalizedAssetId
        ? engagementKnowledgeKeys.asset(normalizedEngagementId, normalizedAssetId)
        : ["engagement", "__disabled__", "asset", "__disabled__"],
    enabled: Boolean(normalizedEngagementId && normalizedAssetId),
    queryFn: ({ signal }) =>
      fetchJson<AssetDetail>(
        `/api/engagements/${normalizedEngagementId}/assets/${normalizedAssetId}`,
        signal,
      ),
  });
}

export function useEngagementEvidence(
  engagementId: string | number | null | undefined,
  filters?: EvidenceFilters,
) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<PaginatedResponse<EvidenceListItem>>({
    queryKey: normalizedEngagementId
      ? engagementKnowledgeKeys.evidence(normalizedEngagementId, normalizedFilters)
      : ["engagement", "__disabled__", "evidence", normalizedFilters],
    enabled: Boolean(normalizedEngagementId),
    queryFn: ({ signal }) =>
      fetchJson<PaginatedResponse<EvidenceListItem>>(
        `/api/engagements/${normalizedEngagementId}/evidence${toQueryString(normalizedFilters)}`,
        signal,
      ),
  });
}

export function useEngagementGraph(engagementId: string | number | null | undefined) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  return useQuery<EngagementGraphSnapshot>({
    queryKey: normalizedEngagementId
      ? engagementKnowledgeKeys.graph(normalizedEngagementId)
      : ["engagement", "__disabled__", "graph"],
    enabled: Boolean(normalizedEngagementId),
    queryFn: ({ signal }) =>
      fetchJson<EngagementGraphSnapshot>(
        `/api/engagements/${normalizedEngagementId}/relationships/graph`,
        signal,
      ),
  });
}

export function useEngagementWebSurfaceOrigins(
  engagementId: string | number | null | undefined,
  serviceKey: string | null | undefined,
  filters?: WebSurfaceOriginsFilters,
) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  const normalizedServiceKey = normalizeEngagementId(serviceKey);
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<{ service_key: string; items: WebSurfaceOriginSummary[] }>({
    queryKey:
      normalizedEngagementId && normalizedServiceKey
        ? engagementKnowledgeKeys.webSurfaceOrigins(
            normalizedEngagementId,
            normalizedServiceKey,
            Boolean(normalizedFilters.include_noisy),
          )
        : ["engagement", "__disabled__", "web-surface", "origins", "__disabled__"],
    enabled: Boolean(normalizedEngagementId && normalizedServiceKey),
    queryFn: async ({ signal }) => {
      const payload = await fetchJson<Partial<{ service_key: string; items: WebSurfaceOriginSummary[] }>>(
        `/api/engagements/${normalizedEngagementId}/web-surface${toQueryString({
          service_key: normalizedServiceKey as string,
          ...normalizedFilters,
        })}`,
        signal,
      );
      return normalizeWebSurfaceOriginsResponse(payload, normalizedServiceKey as string);
    },
  });
}

export function useEngagementWebSurfacePathPage(
  engagementId: string | number | null | undefined,
  serviceKey: string | null | undefined,
  filters?: WebSurfacePathFilters,
) {
  const normalizedEngagementId = normalizeEngagementId(engagementId);
  const normalizedServiceKey = normalizeEngagementId(serviceKey);
  const normalizedFilters = normalizeFilterRecord(filters);
  return useQuery<WebSurfacePathPage>({
    queryKey:
      normalizedEngagementId && normalizedServiceKey
        ? engagementKnowledgeKeys.webSurfacePaths(
            normalizedEngagementId,
            normalizedServiceKey,
            normalizedFilters,
          )
        : ["engagement", "__disabled__", "web-surface", "paths", "__disabled__", normalizedFilters],
    enabled: Boolean(normalizedEngagementId && normalizedServiceKey),
    queryFn: async ({ signal }) => {
      const payload = await fetchJson<Partial<WebSurfacePathPage>>(
        `/api/engagements/${normalizedEngagementId}/web-surface/paths${toQueryString({
          service_key: normalizedServiceKey as string,
          ...normalizedFilters,
        })}`,
        signal,
      );
      return normalizeWebSurfacePathPageResponse(
        payload,
        normalizedServiceKey as string,
        normalizedFilters,
      );
    },
  });
}
